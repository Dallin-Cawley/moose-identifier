"""How each photo is identified. See SORTING_RULES.md, "Identification strategies".

A strategy builds the request for one photo (which images, which questions) and turns the answers
into a Result. Only `single` exists today: sending neighbouring frames as well was tested and
dropped (about 5x the cost for no clear gain; see SORTING_RULES.md). After all photos are
identified, `smooth` and `flag_for_review` revise results using the other photos of the same
event. That is code only, with no extra API calls.
"""

import dataclasses
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from typesafe_sdk import Image

from .classify import Category, Result, decide, has_moose_features, prepare_image, read_answers
from .events import Event
from .questions import MODEL, MOOSE, SINGLE_QUESTIONS, SMOOTH_MIN_MOOSE, YES

# Measured input tokens per photo (banner cropped, 1568x833, with this question set): ~4,380.
TOKENS_PER_IMAGE = 4_400


@dataclass(frozen=True)
class Neighbours:
    before: tuple[Path, ...] = ()   # nearest first
    after: tuple[Path, ...] = ()    # nearest first

    def all(self) -> tuple[Path, ...]:
        return self.before + self.after


def find_neighbours(photos: Sequence[tuple[Path, Event]], k: int) -> dict[Path, Neighbours]:
    """Up to k photos before and after each photo, from the same event only.

    `photos` must be in time order (as returned by group_into_events).
    """
    by_event: dict[Event, list[Path]] = defaultdict(list)
    for path, event in photos:
        by_event[event].append(path)
    out = {}
    for paths in by_event.values():
        for i, p in enumerate(paths):
            out[p] = Neighbours(before=tuple(reversed(paths[max(0, i - k):i])), after=tuple(paths[i + 1:i + 1 + k]))
    return out


def _prepare(path: Path) -> bytes:
    return prepare_image(path, crop_banner=True)


def result_from_answers(photo: Path, answers, model: str, input_tokens: int | None) -> Result:
    """The production reading of one response (also used to replay recorded answers in tests)."""
    animal, species, features = read_answers(answers)
    top = max(species, key=species.get)
    category = decide(animal, species[MOOSE], top, has_moose_features(features))
    return Result(photo, category, animal, species[MOOSE], top, model, input_tokens, features)


class IdentificationStrategy(ABC):
    name: str
    description: str
    images_per_photo: int = 1  # for the cost estimate

    @abstractmethod
    def build(self, photo: Path) -> tuple[dict, dict]:
        """(state, questions) for one photo."""

    async def identify(self, client, photo: Path) -> Result:
        """Raises on API failure; a failed call is never treated as an answer."""
        state, questions = self.build(photo)
        kwargs = {}
        if hasattr(client, "spent"):  # budget-guarded client (scripts): log which photo and how many images
            kwargs = {"arm": self.name, "photo": photo.name, "images": self.images_per_photo}
        response = await client.system_one(state=state, questions=questions, model=MODEL, **kwargs)
        return result_from_answers(photo, response.answers, response.model, response.usage.input_tokens)


class SinglePhoto(IdentificationStrategy):
    name = "single"
    description = "each photo on its own (1 image per photo)"

    def build(self, photo):
        return {"photo": Image(_prepare(photo))}, SINGLE_QUESTIONS


STRATEGIES: dict[str, type[IdentificationStrategy]] = {s.name: s for s in (SinglePhoto,)}


# --- event smoothing (code only) -------------------------------------------------------------

SMOOTH_NEIGHBOURS = 1  # compare with the frame just before and just after, within the same event


def smooth(results: dict[Path, Result], neighbours: dict[Path, Neighbours]) -> dict[Path, Result]:
    """Promote a photo to `animal/moose` when the frame next to it in the same event is a moose.

    Only when all of these hold:
    - the photo itself is not already moose, and Jev is sure it shows an animal (animal > YES);
    - a neighbouring frame (±SMOOTH_NEIGHBOURS, same event) was identified as moose;
    - Jev did not confidently call it another species: P(moose) >= SMOOTH_MIN_MOOSE, or moose
      anatomy is visible.
    Moose seen from behind are the usual case: the frames around them show the head.
    Uses only the original results of the neighbours (no chains of promotions).
    """
    out = dict(results)
    for path, r in results.items():
        if r.category not in (Category.OTHER_ANIMAL, Category.UNSURE) or r.animal <= YES:
            continue
        nb = neighbours.get(path, Neighbours())
        near = nb.before[:SMOOTH_NEIGHBOURS] + nb.after[:SMOOTH_NEIGHBOURS]
        if not any(results.get(p) is not None and results[p].category == Category.MOOSE for p in near):
            continue
        if r.moose >= SMOOTH_MIN_MOOSE or has_moose_features(r.features):
            out[path] = dataclasses.replace(r, category=Category.MOOSE, smoothed=True)
    return out


# --- review folder (code only) ---------------------------------------------------------------

def flag_for_review(results: dict[Path, Result], event_of: dict[Path, Event]) -> dict[Path, Result]:
    """Move `animal/not_moose` photos to `animal/not_moose_check` when their event also has a moose photo.

    On the test set, every moose filed as "not a moose" was in an event with moose photos (a moose
    walking away from the camera), while real deer were in events without moose. The check folder
    makes those few photos easy to look through. Run after `smooth`.
    """
    moose_events = {event_of[p] for p, r in results.items() if r.category == Category.MOOSE}
    return {p: dataclasses.replace(r, category=Category.CHECK_NOT_MOOSE)
            if r.category == Category.OTHER_ANIMAL and event_of.get(p) in moose_events else r
            for p, r in results.items()}

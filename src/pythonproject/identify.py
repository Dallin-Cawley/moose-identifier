"""How each photo is labelled. See SORTING_RULES.md, "Labelling".

A strategy builds the request for one photo (which images, which questions) and turns the answers
into Labels. Only `single` exists: sending neighbouring frames as well was tested and dropped
(about 5x the cost for no clear gain). After all photos are labelled, `smooth` revises them using
the frames next to them. That is code only, with no extra API calls.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from typesafe_sdk import Image

from .classify import prepare_image
from .events import WINDOW
from .labels import Labels, read_answers, with_smoothed
from .questions import MODEL, SINGLE_QUESTIONS

# Measured input tokens per photo (banner cropped, 1568x833, current questions): 4,401 on 1,338 photos.
TOKENS_PER_IMAGE = 4_400


def _prepare(path: Path) -> bytes:
    return prepare_image(path, crop_banner=True)


def labels_from_answers(photo: Path, answers, model: str, input_tokens: int | None) -> Labels:
    """The production reading of one response (also used to replay recorded answers)."""
    return Labels(photo, read_answers(answers), model, input_tokens)


class IdentificationStrategy(ABC):
    name: str
    description: str
    images_per_photo: int = 1  # for the cost estimate

    @abstractmethod
    def build(self, photo: Path) -> tuple[dict, dict]:
        """(state, questions) for one photo."""

    async def identify(self, client, photo: Path) -> Labels:
        """Raises on API failure; a failed call is never treated as an answer."""
        state, questions = self.build(photo)
        kwargs = {}
        if hasattr(client, "spent"):  # budget-guarded client (scripts): log which photo and how many images
            kwargs = {"arm": self.name, "photo": photo.name, "images": self.images_per_photo}
        response = await client.system_one(state=state, questions=questions, model=MODEL, **kwargs)
        return labels_from_answers(photo, response.answers, response.model, response.usage.input_tokens)


class SinglePhoto(IdentificationStrategy):
    name = "single"
    description = "each photo on its own (1 image per photo)"

    def build(self, photo):
        return {"photo": Image(_prepare(photo))}, SINGLE_QUESTIONS


STRATEGIES: dict[str, type[IdentificationStrategy]] = {s.name: s for s in (SinglePhoto,)}


# --- smoothing (code only) -----------------------------------------------------------------------

def smooth(labels: dict[Path, Labels], timeline: Sequence[tuple[Path, datetime]]) -> dict[Path, Labels]:
    """Confirm an unsettled animal when the frame right before or after shows it for sure.

    `timeline` is every photo of the run in time order, including those whose call failed.
    A photo gains animal A only when all of these hold:
    - Jev was unsettled about A in this photo (NO <= P(A) <= YES), not sure it's absent;
    - the photo directly before or after it in time, less than WINDOW away, has A confirmed (> YES).
    Moose seen from behind are the usual case: the frames around them show the head.
    Only the neighbours' own answers count, so additions never chain along a sequence.
    """
    out = dict(labels)
    for i, (path, taken) in enumerate(timeline):
        own = labels.get(path)
        if own is None or not own.unsettled:
            continue
        borrowed = set()
        for j in (i - 1, i + 1):
            if not 0 <= j < len(timeline):
                continue
            other_path, other_taken = timeline[j]
            other = labels.get(other_path)
            if other is not None and abs(other_taken - taken) < WINDOW:
                borrowed |= set(own.unsettled) & set(other.confirmed)
        if borrowed:
            out[path] = with_smoothed(own, borrowed)
    return out

"""Group labelled photos into events, following SORTING_RULES.md (which has examples).

- Only photos with an animal take part; photos without one belong to no event. A photo with an
  animal Jev couldn't identify (UNIDENTIFIED) takes part too: it keeps the event going.
- An event has no maximum length. It ends when WINDOW (10 minutes) passes with no animal detected:
  a photo with an animal taken WINDOW or more after the previous one starts a new event.
- The event's name is its animals joined by "-": moose first (SORTING_RULES.md: `moose` and
  `moose-<others>`), then the others in SORTING_RULES.md order (human, dog, cougar, deer, elk).
  Unidentified photos don't add to the name; an event of only those is named `unidentified`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from .questions import ANIMALS, MOOSE, UNIDENTIFIED

WINDOW = timedelta(minutes=10)

_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 0x9003
_DATETIME = 0x0132


def read_timestamp(path: Path) -> datetime:
    """Capture time from EXIF (DateTimeOriginal, else DateTime). Raises ValueError if absent."""
    with Image.open(path) as im:
        exif = im.getexif()
        raw = exif.get_ifd(_EXIF_IFD).get(_DATETIME_ORIGINAL) or exif.get(_DATETIME)
    if not raw:
        raise ValueError("no EXIF capture time")
    return datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S")


def event_name(animals) -> str:
    """'moose', 'moose-deer', 'deer-elk', ...: moose first, then SORTING_RULES.md order.

    'unidentified' only when no animal of SORTING_RULES.md was identified anywhere in the event.
    """
    present = set(animals)
    order = [MOOSE] + [a for a in ANIMALS if a != MOOSE]
    return "-".join(a for a in order if a in present) or (UNIDENTIFIED if UNIDENTIFIED in present else "")


@dataclass(frozen=True)
class Event:
    start: datetime                 # first photo with an animal
    end: datetime                   # last photo with an animal (inclusive)
    animals: tuple[str, ...]        # in event_name order
    photos: tuple[Path, ...]        # in time order

    @property
    def name(self) -> str:
        return event_name(self.animals)

    @property
    def is_moose(self) -> bool:
        """Moose event: only moose. A moose with others is a mixed event (SORTING_RULES.md 2.2)."""
        return self.animals == (MOOSE,)

    @property
    def is_mixed(self) -> bool:
        return MOOSE in self.animals and len(self.animals) > 1

    @property
    def folder(self) -> str:
        """Start time first, so folders sort in time order and repeated names stay unique."""
        return f"{self.start:%Y-%m-%d_%H%M%S}_{self.name}"


def group_events(photos: Sequence[tuple[Path, datetime, Sequence[str]]],
                 window: timedelta = WINDOW) -> list[Event]:
    """(path, capture time, labels) for each photo -> events, in time order.

    labels: the identified animals, or ("unidentified",); empty = no animal, left out.
    Input order doesn't matter.
    """
    with_animals = sorted(((t, p, tuple(a)) for p, t, a in photos if a), key=lambda x: x[0])
    groups: list[list[tuple[datetime, Path, tuple[str, ...]]]] = []
    for item in with_animals:
        if not groups or item[0] - groups[-1][-1][0] >= window:
            groups.append([])
        groups[-1].append(item)
    events = []
    for g in groups:
        animals = {a for _, _, labels in g for a in labels}
        order = event_name(animals).split("-")
        events.append(Event(g[0][0], g[-1][0], tuple(order), tuple(p for _, p, _ in g)))
    return events

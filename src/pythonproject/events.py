"""Group photos into time-based events. See SORTING_RULES.md for the rules."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

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


@dataclass(frozen=True)
class Event:
    index: int
    start: datetime
    end: datetime  # exclusive

    @property
    def folder(self) -> str:
        return f"event_{self.index:04d}_{self.start:%Y-%m-%d_%H%M%S}"


class EventStrategy(ABC):
    name: str

    def __init__(self, window: timedelta = WINDOW) -> None:
        self.window = window

    @abstractmethod
    def assign(self, timestamps: Sequence[datetime]) -> list[Event]:
        """Return one Event per timestamp, in the same order as the input."""


class StaticWindows(EventStrategy):
    """Fixed grid anchored at the earliest photo; index = window number (gaps where empty)."""

    name = "static"

    def assign(self, timestamps: Sequence[datetime]) -> list[Event]:
        if not timestamps:
            return []
        anchor = min(timestamps)
        events = []
        for t in timestamps:
            index = (t - anchor) // self.window
            start = anchor + index * self.window
            events.append(Event(index, start, start + self.window))
        return events


class RollingWindows(EventStrategy):
    """A new event starts at the first photo not covered by the previous event's window."""

    name = "rolling"

    def assign(self, timestamps: Sequence[datetime]) -> list[Event]:
        events: list[Event | None] = [None] * len(timestamps)
        current: Event | None = None
        for i in sorted(range(len(timestamps)), key=timestamps.__getitem__):
            t = timestamps[i]
            if current is None or t >= current.end:
                index = 0 if current is None else current.index + 1
                current = Event(index, t, t + self.window)
            events[i] = current
        return events  # type: ignore[return-value]


STRATEGIES: dict[str, type[EventStrategy]] = {s.name: s for s in (StaticWindows, RollingWindows)}

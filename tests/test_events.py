"""Event grouping: static and rolling 10-minute windows."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import IMAGES, requires_images
from pythonproject import find_images, group_into_events
from pythonproject.events import STRATEGIES, RollingWindows, StaticWindows, read_timestamp

BASE = datetime(2024, 10, 8, 9, 3, 13)


def at(*minutes: float) -> list[datetime]:
    return [BASE + timedelta(minutes=m) for m in minutes]


def indices(strategy, minutes):
    return [e.index for e in strategy.assign(at(*minutes))]


class TestStatic:
    def test_grid_from_first_photo(self):
        assert indices(StaticWindows(), [0, 9.99, 10, 310]) == [0, 0, 1, 31]

    def test_boundary_photo_starts_new_event(self):
        # the user's example: a moose at minutes 8..11 is split at minute 10
        assert indices(StaticWindows(), [0, 8, 9, 10, 11]) == [0, 0, 0, 1, 1]

    def test_window_bounds(self):
        e = StaticWindows().assign(at(0, 25))[1]
        assert (e.start, e.end) == (BASE + timedelta(minutes=20), BASE + timedelta(minutes=30))

    def test_anchor_is_earliest_not_first_given(self):
        assert indices(StaticWindows(), [25, 0]) == [2, 0]


class TestRolling:
    def test_new_event_at_first_photo_outside_window(self):
        events = RollingWindows().assign(at(0, 9.99, 10, 15, 30.02))
        assert [e.index for e in events] == [0, 0, 1, 1, 2]
        assert [events[i].start for i in (0, 2, 4)] == at(0, 10, 30.02)

    def test_moose_crossing_minute_ten(self):
        # event 0 starts at minute 0; the photo at minute 10 starts event 1 and keeps minute 11
        assert indices(RollingWindows(), [0, 8, 9, 10, 11]) == [0, 0, 0, 1, 1]

    def test_differs_from_static(self):
        # a visit at 16..24 is one rolling event but crosses the static line at 20
        mins = [0, 16, 19, 21, 24]
        assert indices(RollingWindows(), mins) == [0, 1, 1, 1, 1]
        assert indices(StaticWindows(), mins) == [0, 1, 1, 2, 2]

    def test_sequential_numbering_without_gaps(self):
        assert indices(RollingWindows(), [0, 500, 10_000]) == [0, 1, 2]


@pytest.mark.parametrize("strategy", [StaticWindows(), RollingWindows()], ids=lambda s: s.name)
def test_unsorted_input_keeps_input_order(strategy):
    mins = [15, 0, 30.02, 10, 9.99]
    by_time = sorted(range(5), key=lambda i: mins[i])
    events = strategy.assign(at(*mins))
    expected = strategy.assign(at(*sorted(mins)))
    assert [events[i] for i in by_time] == expected


@pytest.mark.parametrize("strategy", [StaticWindows(), RollingWindows()], ids=lambda s: s.name)
def test_empty_input(strategy):
    assert strategy.assign([]) == []


def test_folder_name():
    e = StaticWindows().assign(at(0))[0]
    assert e.folder == "event_0000_2024-10-08_090313"


def test_strategy_registry():
    assert set(STRATEGIES) == {"static", "rolling"}


def test_read_timestamp(make_photo):
    assert read_timestamp(make_photo("a.jpg", BASE)) == BASE


def test_read_timestamp_missing(make_photo):
    with pytest.raises(ValueError):
        read_timestamp(make_photo("a.jpg", None))


@pytest.fixture(scope="module")
def images() -> list[Path]:
    return find_images([IMAGES], exclude=Path("/nonexistent"))


@requires_images
class TestRealPhotoSet:
    """Facts about the photos in src/images, documented in SORTING_RULES.md."""

    def test_every_photo_has_a_timestamp(self, images):
        photos, missing = group_into_events(images, StaticWindows())
        assert len(images) == 5061 and missing == []

    def test_event_counts(self, images):
        assert len({e for _, e in group_into_events(images, StaticWindows())[0]}) == 61
        assert len({e for _, e in group_into_events(images, RollingWindows())[0]}) == 56

    def test_documented_static_example(self, images):
        photos, _ = group_into_events(images, StaticWindows())
        e = photos[0][1]
        assert e.folder == "event_0000_2024-10-08_090313"
        anchor = e.start
        t = datetime(2024, 10, 30, 16, 31, 40)
        assert (t - anchor) // timedelta(minutes=10) == 3212

"""Events from labelled photos, per SORTING_RULES.md: an event ends after 10 minutes without an animal."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import IMAGES, requires_images
from pythonproject import find_images, read_timestamps
from pythonproject.events import WINDOW, event_name, group_events, read_timestamp

BASE = datetime(2024, 10, 30, 16, 28, 4)


def photos(*spec):
    """spec: (minutes after BASE, "moose,deer" or "" for no animal)."""
    return [(Path(f"p{i}.jpg"), BASE + timedelta(minutes=m), tuple(a for a in labels.split(",") if a))
            for i, (m, labels) in enumerate(spec)]


def groups(*spec):
    return [[p.name for p in e.photos] for e in group_events(photos(*spec))]


def test_photos_without_animals_belong_to_no_event():
    assert groups((0, ""), (1, "moose"), (2, ""), (3, "moose"), (30, "")) == [["p1.jpg", "p3.jpg"]]


def test_no_animals_no_events():
    assert group_events(photos((0, ""), (5, ""))) == []
    assert group_events([]) == []


def test_ten_minutes_without_an_animal_ends_the_event():
    assert groups((0, "moose"), (9.99, "moose"), (19.98, "moose")) == [["p0.jpg", "p1.jpg", "p2.jpg"]]
    assert groups((0, "moose"), (10, "moose")) == [["p0.jpg"], ["p1.jpg"]]   # exactly 10 min: new event


def test_no_maximum_length():
    # a moose every 5 minutes for 3 hours is one event
    spec = [(m, "moose") for m in range(0, 180, 5)]
    [e] = group_events(photos(*spec))
    assert e.end - e.start == timedelta(minutes=175)


def test_empty_frames_dont_keep_an_event_open():
    # empty frames at 5 and 12 don't count as activity: the moose at 14 starts a new event
    assert groups((0, "moose"), (5, ""), (12, ""), (14, "moose")) == [["p0.jpg"], ["p3.jpg"]]


def test_input_order_does_not_matter():
    spec = [(12, "deer"), (0, "moose"), (30, "elk"), (3, "moose")]
    assert groups(*spec) == [["p1.jpg", "p3.jpg", "p0.jpg"], ["p2.jpg"]]


@pytest.mark.parametrize(("animals", "name"), [
    ({"moose"}, "moose"),                                        # Moose Event
    ({"deer", "moose"}, "moose-deer"),                           # Mixed Event: moose first
    ({"elk", "human", "moose", "dog"}, "moose-human-dog-elk"),   # then SORTING_RULES.md order
    ({"deer"}, "deer"),                                          # no moose: animals in SORTING_RULES.md order
    ({"elk", "cougar", "human"}, "human-cougar-elk"),
    ({"moose", "unidentified"}, "moose"),                        # unidentified frames don't name it
    ({"unidentified"}, "unidentified"),                          # ...unless nothing was identified
])
def test_event_name(animals, name):
    assert event_name(animals) == name


def test_event_collects_every_animal_and_has_a_dated_folder():
    [e] = group_events(photos((0, "deer"), (2, "moose"), (4, "deer,human")))
    assert e.animals == ("moose", "human", "deer") and e.name == "moose-human-deer"
    assert e.folder == "2024-10-30_162804_moose-human-deer"
    assert e.is_mixed and not e.is_moose
    assert (e.start, e.end) == (BASE, BASE + timedelta(minutes=4))


def test_unidentified_frames_keep_the_event_going():
    # a moose seen from behind at 8 and 16 isn't identified, but it's an animal: one event, named moose
    [e] = group_events(photos((0, "moose"), (8, "unidentified"), (16, "unidentified"), (20, "moose")))
    assert e.name == "moose" and len(e.photos) == 4


def test_event_types():
    moose, deer = group_events(photos((0, "moose"), (1, "moose"), (60, "deer")))
    assert moose.is_moose and not moose.is_mixed
    assert not deer.is_moose and not deer.is_mixed


def test_window_is_ten_minutes():
    assert WINDOW == timedelta(minutes=10)


def test_read_timestamp(make_photo):
    assert read_timestamp(make_photo("a.jpg", BASE)) == BASE


def test_read_timestamp_missing(make_photo):
    with pytest.raises(ValueError):
        read_timestamp(make_photo("a.jpg", None))


@requires_images
def test_every_photo_of_the_card_has_a_timestamp():
    images = find_images([IMAGES], exclude=Path("/nonexistent"))
    stamped, missing = read_timestamps(images)
    assert len(images) == 5061 and missing == []

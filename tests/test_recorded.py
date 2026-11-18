"""Replay Jev's recorded answers to the production questions through the production code (no API calls).

- dev (232 photos): for tuning.
- test (200 photos): random photos labelled before Jev saw them; for the final check only.
`ambiguous` labels are not scored. See tests/replay.py and SORTING_RULES.md.

These tests skip when there are no recordings for the current question wording. Record them with
scripts/experiment.py (about $0.04 per set, plus its `nb` neighbours for smoothing), then update
BASELINE from `python tests/replay.py`.
Changing thresholds or smooth() is then measured here for free; changing question wording makes the
recordings stale and needs new ones.
"""

import json

import pytest

import replay as R
from pythonproject.questions import SINGLE_QUESTIONS

# (set): (min moose found, max moose missed), with smoothing, from the recordings of 2026-11-18
# (jev-1.14.0). Raise them when a change improves things; don't loosen them without knowing why.
BASELINE: dict[str, tuple[int, int]] = {"dev": (85, 2), "test": (107, 5)}

recorded = [s for s in R.LABELS if R.recording(s) is not None]
needs_recording = pytest.mark.skipif(not recorded, reason="no recordings with the current questions yet")


@needs_recording
def test_recordings_asked_the_production_questions():
    used = json.loads((R.EXP / "questions_used.json").read_text())[R.ARM]
    for qid, q in SINGLE_QUESTIONS.items():
        assert used[qid] == q.model_dump(mode="json"), f"{qid} changed: re-record with scripts/experiment.py"


@needs_recording
@pytest.mark.parametrize("set_name", recorded)
def test_every_labelled_photo_was_recorded(set_name):
    assert set(R.recording(set_name)) >= set(R.labels(set_name))


@needs_recording
@pytest.mark.parametrize("set_name", recorded)
def test_no_false_moose(set_name):
    """A moose event must really have a moose: no deer, person or empty frame labelled moose."""
    assert R.metrics(set_name, R.results(set_name))["false_moose"] == 0


@needs_recording
@pytest.mark.parametrize("set_name", recorded)
def test_every_animal_is_detected(set_name):
    """Tier 1 decides the events: no moose, deer, raccoon or bird photo may be left out."""
    m = R.metrics(set_name, R.results(set_name))
    assert m["animal_found"] == m["animal_total"] and m["moose_dropped"] == 0, m
    assert m["empty_given_animal"] == 0, m


@needs_recording
@pytest.mark.parametrize("set_name", sorted(BASELINE))
def test_baseline(set_name):
    min_found, max_missed = BASELINE[set_name]
    m = R.metrics(set_name, R.results(set_name))
    assert m["moose_found"] >= min_found and m["moose_missed"] <= max_missed, m

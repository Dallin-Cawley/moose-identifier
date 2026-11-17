"""Replay Jev's recorded answers through the production code (no API calls).

- dev (232 photos): the photos the setup was tuned on.
- test (200 photos): random photos labelled before Jev saw them, used once to check the final setup.
`ambiguous` labels (no one could tell) are not scored. See tests/replay.py and SORTING_RULES.md.

Changing thresholds, decide() or smooth() is measured here for free. Changing the question wording
makes the recordings stale (first test) and needs new recordings via scripts/experiment.py.
"""

import json

import pytest

import replay as R
from pythonproject.questions import SINGLE_QUESTIONS

# Baselines from the recordings of 2026-11-16 (jev-1.14.0). Raise them when a change improves
# things; don't loosen them without understanding why.
#   (set, configuration): (min correct, max hard misses, max people sorted as animals)
# Hard misses = moose filed in animal/not_moose or no_animal (animal/not_moose_check is a review folder).
BASELINE = {
    ("dev", "single + smoothing"): (210, 0, 0),
    ("test", "single + smoothing"): (186, 0, 0),
}

needs_card = pytest.mark.skipif(not R.IMAGES.is_dir(), reason="smoothing replay needs src/images for events")


def test_recordings_asked_the_production_questions():
    used = json.loads((R.EXP / "questions_used.json").read_text())[R.ARMS["single"][0]]
    for qid, q in SINGLE_QUESTIONS.items():
        assert used[qid] == q.model_dump(mode="json"), f"{qid} changed: re-record with scripts/experiment.py"


@pytest.mark.parametrize("set_name", ["dev", "test"])
def test_every_labelled_photo_was_recorded(set_name):
    for arm, _ in R.ARMS.values():
        assert set(R.recording(set_name, arm)) == set(R.labels(set_name))


def configs():
    return {(s, name): rs for s in ("dev", "test") for name, rs in R.configurations(s)}


@needs_card
@pytest.mark.parametrize("key", BASELINE, ids=lambda k: f"{k[0]}:{k[1]}")
def test_baseline(key):
    min_correct, max_hard_miss, max_people = BASELINE[key]
    m = R.metrics(key[0], configs()[key])
    assert m["correct"] >= min_correct, m
    assert m["hard_miss"] <= max_hard_miss, m
    assert m["people_to_animal"] <= max_people, m


@needs_card
@pytest.mark.parametrize("key", BASELINE, ids=lambda k: f"{k[0]}:{k[1]}")
def test_no_false_moose(key):
    """The moose folder must stay clean: no deer, raccoon, bird, person or empty frame in it."""
    assert R.metrics(key[0], configs()[key])["false_moose"] == 0


@needs_card
def test_smoothing_only_ever_promotes_to_moose():
    for s in ("dev", "test"):
        raw = R.results(s, "single", with_context=True)
        after = R.smoothed(raw)
        changed = [p for p in raw if after[p].category != raw[p].category]
        assert changed and all(after[p].category.value == "animal/moose" and after[p].smoothed for p in changed)


@needs_card
def test_check_folder_holds_no_real_deer_on_dev():
    """Dev has 61 deer/raccoon/bird photos; none should need reviewing (they're in moose-free events)."""
    lab = R.labels("dev")
    for name, rs in R.configurations("dev"):
        flagged = [p.name for p, r in rs.items() if r.category.value == "animal/not_moose_check" and p.name in lab]
        assert not [p for p in flagged if lab[p] != "animal/moose"], name


@needs_card
def test_every_test_hard_miss_is_caught_by_the_check_folder():
    m = R.metrics("test", dict(R.configurations("test"))["single + smoothing"])
    assert (m["hard_miss"], m["check_moose"], m["check_folder"]) == (0, 5, 5)

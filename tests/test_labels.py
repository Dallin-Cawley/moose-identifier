"""From Jev's answers to a photo's labels, and the label store that caches them.

Tier 1: is there an animal? Tier 2: is it a moose? Tier 3: which other animals?
"""

import json

import pytest

from conftest import make_labels as lab
from pythonproject.labels import LabelStore, file_sha256, with_smoothed
from pythonproject.questions import NO, NO_ANIMAL, SMOOTH_MIN_MOOSE, UNIDENTIFIED, YES


# --- the truth table -------------------------------------------------------------------------------
# moose=0.4: with the rest of the species answer mostly on deer (0.48), moose is not the top option.

@pytest.mark.parametrize(("answers", "has_animal", "animals", "keywords"), [
    # tier 1 + 2: an animal, and moose is the top species
    (dict(animal=0.95, moose=0.9), True, ("moose",), ("moose",)),
    (dict(animal=0.95, moose=0.45), True, ("moose",), ("moose",)),     # top option even under 0.5
    # an animal, moose not on top, no other animal confirmed: kept, but unidentified
    (dict(animal=0.95, moose=0.05), True, (), (UNIDENTIFIED,)),
    (dict(animal=0.95, moose=0.4), True, (), (UNIDENTIFIED,)),
    # moose on top, but Jev isn't sure there is an animal: no moose, no animal
    (dict(animal=0.6, moose=0.9), False, (), (NO_ANIMAL,)),
    (dict(animal=YES, moose=0.9), False, (), (NO_ANIMAL,)),            # exactly 0.7 is not yet a yes
    # tier 3: other animals from their own question
    (dict(animal=0.95, moose=0.05, deer=0.9), True, ("deer",), ("deer",)),
    (dict(animal=0.95, moose=0.9, deer=0.8), True, ("deer", "moose"), ("deer", "moose")),
    # a person: `animal` leaves people out, the `human` question counts them
    (dict(animal=0.05, human=0.9), True, ("human",), ("human",)),
    (dict(animal=0.9, human=0.9, dog=0.85), True, ("human", "dog"), ("human", "dog")),
    # a confirmed animal counts as an animal even if `animal` is unsettled
    (dict(animal=0.5, cougar=0.8), True, ("cougar",), ("cougar",)),
    # nothing
    (dict(), False, (), (NO_ANIMAL,)),
    (dict(animal=0.5, elk=0.5), False, (), (NO_ANIMAL,)),
])
def test_truth_table(answers, has_animal, animals, keywords):
    r = lab(**answers)
    assert (r.has_animal, r.animals, r.keywords) == (has_animal, animals, keywords)


@pytest.mark.parametrize(("answers", "unsettled"), [
    (dict(animal=0.95, moose=0.4), ("moose",)),                 # moose plausible, not on top
    (dict(animal=0.95, moose=SMOOTH_MIN_MOOSE), ("moose",)),
    (dict(animal=0.95, moose=SMOOTH_MIN_MOOSE - 0.01), ()),     # Jev confidently said another species
    (dict(animal=0.6, moose=0.4), ()),                          # needs a sure animal
    (dict(animal=0.95, moose=0.9), ()),                         # already confirmed
    (dict(animal=0.95, moose=0.05, deer=NO, elk=YES), ("deer", "elk")),
    (dict(animal=0.95, moose=0.05, deer=NO - 0.01), ()),
])
def test_unsettled(answers, unsettled):
    assert lab(**answers).unsettled == unsettled


def test_smoothed_animals_join_the_labels():
    r = with_smoothed(lab(animal=0.95, moose=0.4, deer=0.9), ["moose"])
    assert r.animals == ("deer", "moose") and r.confirmed == ("deer",) and r.smoothed == ("moose",)


def test_read_answers_averages_both_species_orders(fake_api):
    from types import SimpleNamespace as NS
    from pythonproject.labels import read_answers
    answers = {"animal": NS(noul=0.9), "species": NS(probabilities={"moose": 0.6, "deer": 0.4}),
               "species_rev": NS(probabilities={"deer": 0.8, "moose": 0.2}),
               **{a: NS(noul=0.1) for a in ("human", "dog", "cougar", "deer", "elk")}}
    p = read_answers(answers)
    assert p["species"] == pytest.approx({"moose": 0.4, "deer": 0.6}) and p["animal"] == 0.9


# --- the label store -----------------------------------------------------------------------------

@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"photo bytes")
    return p


def test_store_round_trip(tmp_path, photo):
    store = LabelStore(tmp_path / "labels.jsonl")
    assert store.cached(photo) is None
    store.record(lab(photo, animal=0.95, moose=0.9))
    again = LabelStore(tmp_path / "labels.jsonl").cached(photo)
    assert again.cached and again.input_tokens is None
    assert again.moose == 0.9 and again.animals == ("moose",)


def test_store_is_keyed_by_content_not_name(tmp_path, photo):
    store = LabelStore(tmp_path / "labels.jsonl")
    store.record(lab(photo, animal=0.95, moose=0.9))
    moved = tmp_path / "elsewhere" / "renamed.jpg"
    moved.parent.mkdir()
    moved.write_bytes(photo.read_bytes())
    assert LabelStore(tmp_path / "labels.jsonl").cached(moved).animals == ("moose",)
    moved.write_bytes(b"different photo")
    assert LabelStore(tmp_path / "labels.jsonl").cached(moved) is None


def test_changed_questions_invalidate_the_cache(tmp_path, photo, monkeypatch):
    store = LabelStore(tmp_path / "labels.jsonl")
    store.record(lab(photo, animal=0.95, moose=0.9))
    import pythonproject.labels as L
    monkeypatch.setattr(L, "questions_fingerprint", lambda: "other-wording")
    stale = LabelStore(tmp_path / "labels.jsonl")
    assert stale.cached(photo) is None
    assert stale.keywords(photo) == ("moose",)          # export still knows the last labels


def test_save_rewrites_one_line_per_photo_with_final_labels(tmp_path, photo):
    path = tmp_path / "labels.jsonl"
    store = LabelStore(path)
    store.record(lab(photo, animal=0.95, moose=0.4))
    store.record(lab(photo, animal=0.95, moose=0.4))    # e.g. labelled twice: last wins
    store.save([with_smoothed(lab(photo, animal=0.95, moose=0.4), ["moose"])])
    [line] = path.read_text().splitlines()
    entry = json.loads(line)
    assert entry["labels"] == ["moose"] and entry["sha256"] == file_sha256(photo)
    assert LabelStore(path).keywords(photo) == ("moose",)


def test_read_only_store_writes_nothing(tmp_path, photo):
    store = LabelStore(tmp_path / "labels.jsonl", writable=False)
    store.record(lab(photo, animal=0.95, moose=0.9))
    store.save([lab(photo, animal=0.95, moose=0.9)])
    assert not (tmp_path / "labels.jsonl").exists()
    assert store.cached(photo).animals == ("moose",)    # but this run still sees it

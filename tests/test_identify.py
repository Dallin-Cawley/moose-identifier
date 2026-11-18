"""The identification strategy (what gets sent) and smoothing (code-only revision)."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient

from conftest import answer, make_labels
from pythonproject.identify import STRATEGIES, SinglePhoto, smooth

T0 = datetime(2024, 10, 30, 22, 50, 0)


def identify(strategy, photo):
    async def go():
        async with AsyncTypeSafeClient(headers={"TypeSafe-Danger-Zone-Images": "true"}) as client:
            return await strategy.identify(client, photo)
    return asyncio.run(go())


def test_registry():
    assert set(STRATEGIES) == {"single"}


def test_single_sends_one_photo(fake_api, make_photo):
    assert SinglePhoto().images_per_photo == 1
    identify(SinglePhoto(), make_photo("a.jpg", T0))
    [sent] = fake_api.requests
    assert set(sent.body["state"]) == {"photo"} and len(sent.assets) == 1


def test_answers_become_labels(fake_api, make_photo):
    fake_api.respond = lambda req: answer(tokens=4321, animal=0.96, moose=0.93, deer=0.81, human=0.5)
    r = identify(SinglePhoto(), make_photo("a.jpg", T0))
    assert r.animals == ("deer", "moose") and r.unsettled == ("human",)
    assert (r.model, r.input_tokens, r.cached) == ("jev-1.14.0", 4321, False)


# --- smoothing -------------------------------------------------------------------------------------
# moose=0.4 / 0.2: an animal is sure, moose plausible but not the top species (deer is).

def lab(name, **answers):
    return make_labels(name, **answers)


def timeline(*minutes):
    return [(Path(str(i + 1)), T0 + timedelta(minutes=m)) for i, m in enumerate(minutes)]


def by_path(*labels):
    return {r.path: r for r in labels}


MOOSE_ = dict(animal=0.95, moose=0.95)


def test_moose_from_behind_next_to_a_moose_is_confirmed():
    rs = by_path(lab("1", **MOOSE_), lab("2", animal=0.95, moose=0.2), lab("3", **MOOSE_))
    out = smooth(rs, timeline(0, 1, 2))
    assert out[Path("2")].animals == ("moose",) and out[Path("2")].smoothed == ("moose",)
    assert out[Path("1")].smoothed == ()


def test_works_for_other_animals_and_keeps_own_labels():
    rs = by_path(lab("1", animal=0.95, moose=0.05, deer=0.95, elk=0.9), lab("2", animal=0.95, deer=0.92, elk=0.4))
    assert smooth(rs, timeline(0, 1))[Path("2")].animals == ("deer", "elk")


def test_confidently_other_species_is_not_made_moose():
    rs = by_path(lab("1", **MOOSE_), lab("2", animal=0.95, moose=0.05))
    assert smooth(rs, timeline(0, 1))[Path("2")].animals == ()


def test_needs_a_sure_animal():
    rs = by_path(lab("1", **MOOSE_), lab("2", animal=0.6, moose=0.4))
    assert smooth(rs, timeline(0, 1))[Path("2")].animals == ()


def test_only_direct_neighbours_count():
    rs = by_path(lab("1", **MOOSE_), lab("2"), lab("3", animal=0.95, moose=0.4))
    assert smooth(rs, timeline(0, 1, 2))[Path("3")].animals == ()


def test_neighbour_must_be_within_ten_minutes():
    rs = by_path(lab("1", **MOOSE_), lab("2", animal=0.95, moose=0.4))
    assert smooth(rs, timeline(0, 10))[Path("2")].animals == ()
    assert smooth(rs, timeline(0, 9.9))[Path("2")].animals == ("moose",)


def test_no_chained_additions():
    # 3 is next to 2, which only becomes moose through smoothing: 3 stays as it was
    rs = by_path(lab("1", **MOOSE_), lab("2", animal=0.95, moose=0.4), lab("3", animal=0.95, moose=0.4))
    out = smooth(rs, timeline(0, 1, 2))
    assert out[Path("2")].animals == ("moose",) and out[Path("3")].animals == ()
    assert out[Path("3")].has_animal                    # still an animal: it stays in the event


def test_failed_neighbour_is_ignored():
    rs = by_path(lab("2", animal=0.95, moose=0.4))      # 1 and 3 failed: no answers
    assert smooth(rs, timeline(0, 1, 2))[Path("2")].animals == ()

"""Identification strategies (what gets sent) and event smoothing (code-only revision)."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typesafe_sdk import AsyncTypeSafeClient

from conftest import answer
from pythonproject.classify import Category, Result
from pythonproject.events import Event, StaticWindows
from pythonproject.identify import (STRATEGIES, Neighbours, SinglePhoto, find_neighbours,
                                    flag_for_review, smooth)
from pythonproject.questions import SMOOTH_MIN_MOOSE

T0 = datetime(2024, 10, 30, 22, 50, 0)


def ev(i):
    return Event(i, T0 + timedelta(minutes=10 * i), T0 + timedelta(minutes=10 * (i + 1)))


# --- neighbour selection -----------------------------------------------------------------------

def test_neighbours_stay_inside_the_event():
    photos = [(Path(f"{n}.jpg"), ev(0 if n < 4 else 1)) for n in range(7)]
    nb = find_neighbours(photos, 2)
    assert nb[Path("2.jpg")] == Neighbours((Path("1.jpg"), Path("0.jpg")), (Path("3.jpg"),))
    assert nb[Path("4.jpg")] == Neighbours((), (Path("5.jpg"), Path("6.jpg")))  # 3.jpg is another event
    assert nb[Path("0.jpg")].before == ()


def test_neighbours_nearest_first():
    photos = [(Path(f"{n}.jpg"), ev(0)) for n in range(5)]
    assert find_neighbours(photos, 2)[Path("2.jpg")] == Neighbours(
        (Path("1.jpg"), Path("0.jpg")), (Path("3.jpg"), Path("4.jpg")))


# --- what each strategy sends ------------------------------------------------------------------

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


def test_rescue_rule_end_to_end(fake_api, make_photo):
    # Jev says deer, but a hump and pale legs are visible: unsure, not "not a moose"
    fake_api.respond = lambda req: answer(0.95, 0.2, features={"f_hump": 0.6, "f_pale_legs": 0.85})
    assert identify(SinglePhoto(), make_photo("a.jpg", T0)).category == Category.UNSURE


# --- smoothing ---------------------------------------------------------------------------------

def res(name, category, animal=0.95, moose=0.3, features=None):
    return Result(Path(name), category, animal, moose, "moose" if category == Category.MOOSE else "deer",
                  "jev-1.14.0", 0, features or {})


def chain(*results):
    photos = [(r.path, ev(0)) for r in results]
    return {r.path: r for r in results}, find_neighbours(photos, 1)


def test_frame_between_moose_frames_is_promoted():
    rs, nb = chain(res("1", Category.MOOSE), res("2", Category.OTHER_ANIMAL, moose=0.2), res("3", Category.MOOSE))
    out = smooth(rs, nb)
    assert out[Path("2")].category == Category.MOOSE and out[Path("2")].smoothed
    assert not out[Path("1")].smoothed


def test_unsure_frame_next_to_moose_is_promoted():
    rs, nb = chain(res("1", Category.MOOSE), res("2", Category.UNSURE, moose=0.45))
    assert smooth(rs, nb)[Path("2")].category == Category.MOOSE


def test_confident_other_species_is_not_promoted():
    rs, nb = chain(res("1", Category.MOOSE), res("2", Category.OTHER_ANIMAL, moose=SMOOTH_MIN_MOOSE / 2))
    assert smooth(rs, nb)[Path("2")].category == Category.OTHER_ANIMAL


def test_low_moose_but_moose_anatomy_is_promoted():
    feats = {"f_hump": 0.7, "f_pale_legs": 0.9, "f_long_muzzle": 0.1}
    rs, nb = chain(res("1", Category.MOOSE), res("2", Category.OTHER_ANIMAL, moose=0.02, features=feats))
    assert smooth(rs, nb)[Path("2")].category == Category.MOOSE


@pytest.mark.parametrize("animal", [0.7, 0.5, 0.1])
def test_needs_a_confident_animal(animal):
    rs, nb = chain(res("1", Category.MOOSE), res("2", Category.UNSURE, animal=animal, moose=0.4))
    assert smooth(rs, nb)[Path("2")].category == Category.UNSURE


def test_no_animal_frames_are_never_promoted():
    rs, nb = chain(res("1", Category.MOOSE), res("2", Category.NO_ANIMAL, animal=0.1, moose=0.5))
    assert smooth(rs, nb)[Path("2")].category == Category.NO_ANIMAL


def test_only_direct_neighbours_count():
    rs, nb = chain(res("1", Category.MOOSE), res("2", Category.NO_ANIMAL, animal=0.1),
                   res("3", Category.OTHER_ANIMAL, moose=0.2))
    assert smooth(rs, nb)[Path("3")].category == Category.OTHER_ANIMAL


def test_never_across_events():
    a, b = res("1", Category.MOOSE), res("2", Category.OTHER_ANIMAL, moose=0.2)
    nb = find_neighbours([(a.path, ev(0)), (b.path, ev(1))], 1)
    assert smooth({a.path: a, b.path: b}, nb)[b.path].category == Category.OTHER_ANIMAL


def test_no_chained_promotions():
    # 3 is next to 2, which only becomes moose through smoothing: 3 must stay as it was
    rs, nb = chain(res("1", Category.MOOSE), res("2", Category.OTHER_ANIMAL, moose=0.2),
                   res("3", Category.OTHER_ANIMAL, moose=0.2))
    out = smooth(rs, nb)
    assert out[Path("2")].category == Category.MOOSE and out[Path("3")].category == Category.OTHER_ANIMAL


def test_missing_neighbour_result_is_ignored():
    rs, nb = chain(res("1", Category.MOOSE), res("2", Category.OTHER_ANIMAL, moose=0.2))
    del rs[Path("1")]  # e.g. its API call failed
    assert smooth(rs, nb)[Path("2")].category == Category.OTHER_ANIMAL



# --- review folder -----------------------------------------------------------------------------

def test_not_moose_in_a_moose_event_goes_to_check_folder():
    rs = {r.path: r for r in (res("1", Category.MOOSE), res("2", Category.OTHER_ANIMAL),
                              res("3", Category.OTHER_ANIMAL), res("4", Category.UNSURE))}
    event_of = {Path("1"): ev(0), Path("2"): ev(0), Path("3"): ev(1), Path("4"): ev(0)}
    out = flag_for_review(rs, event_of)
    assert out[Path("2")].category == Category.CHECK_NOT_MOOSE   # same event as a moose
    assert out[Path("3")].category == Category.OTHER_ANIMAL      # deer-only event
    assert out[Path("4")].category == Category.UNSURE            # only "not a moose" calls move
    assert out[Path("1")].category == Category.MOOSE


def test_check_folder_uses_smoothed_moose():
    # a frame promoted by smoothing makes its event a moose event too
    rs, nb = chain(res("1", Category.MOOSE), res("2", Category.OTHER_ANIMAL, moose=0.2),
                   res("3", Category.OTHER_ANIMAL, moose=0.01))
    out = flag_for_review(smooth(rs, nb), {p: ev(0) for p in rs})
    assert [out[Path(n)].category for n in "123"] == [Category.MOOSE, Category.MOOSE, Category.CHECK_NOT_MOOSE]

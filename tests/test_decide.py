"""The truth table that turns Jev's two answers into a folder.

animal: P(animal present) from the `animal` Noul.
moose:  P(species is moose) from the `species` Choice; species: its top option.
"""

import pytest

from pythonproject.classify import Category as C, decide, has_moose_features
from pythonproject.questions import NO, YES


@pytest.mark.parametrize(("animal", "moose", "species", "expected"), [
    (0.95, 0.92, "moose", C.MOOSE),
    (0.95, 0.40, "moose", C.MOOSE),          # moose is the top option, even under 0.5
    (0.95, 0.05, "deer", C.OTHER_ANIMAL),
    (0.95, 0.29, "deer", C.OTHER_ANIMAL),
    (0.95, 0.45, "deer", C.UNSURE),          # moose plausible but not the top option
    (0.05, 0.05, "deer", C.NO_ANIMAL),       # no animal: species answer ignored
    (0.05, 0.60, "moose", C.NO_ANIMAL),      # ...unless it is strongly moose:
    (0.05, 0.95, "moose", C.UNSURE),         # contradiction
    (0.50, 0.95, "moose", C.UNSURE),         # animal unsettled
    (0.50, 0.05, "deer", C.UNSURE),
])
def test_truth_table(animal, moose, species, expected):
    assert decide(animal, moose, species) == expected


def test_thresholds_are_exclusive():
    # exactly on a threshold is unsettled, not yes/no
    assert decide(YES, 0.0, "deer") == C.UNSURE
    assert decide(NO, 0.0, "deer") == C.UNSURE
    assert decide(0.95, NO, "deer") == C.UNSURE        # moose == 0.3 is not "unlikely"
    assert decide(0.05, YES, "moose") == C.NO_ANIMAL   # moose == 0.7 is not "strongly"


def test_category_values_are_folder_paths():
    assert {c.value for c in C} == {"animal/moose", "animal/not_moose", "animal/not_moose_check",
                                    "no_animal", "unsure"}


def test_moose_anatomy_turns_not_moose_into_unsure():
    assert decide(0.95, 0.2, "deer", moose_features=False) == C.OTHER_ANIMAL
    assert decide(0.95, 0.2, "deer", moose_features=True) == C.UNSURE


def test_moose_anatomy_never_makes_a_moose():
    assert decide(0.95, 0.45, "deer", moose_features=True) == C.UNSURE
    assert decide(0.05, 0.05, "deer", moose_features=True) == C.NO_ANIMAL
    assert decide(0.5, 0.05, "deer", moose_features=True) == C.UNSURE


@pytest.mark.parametrize(("hump", "legs", "muzzle", "expected"), [
    (0.6, 0.8, 0.1, True),    # hump + pale long legs (moose from behind)
    (0.6, 0.1, 0.6, True),    # hump + long muzzle
    (0.6, 0.7, 0.5, False),   # thresholds are exclusive
    (0.5, 0.9, 0.9, False),   # no hump: deer can have pale legs
    (0.9, 0.1, 0.1, False),   # hump alone
])
def test_has_moose_features(hump, legs, muzzle, expected):
    assert has_moose_features({"f_hump": hump, "f_pale_legs": legs, "f_long_muzzle": muzzle}) is expected
    assert has_moose_features({}) is False

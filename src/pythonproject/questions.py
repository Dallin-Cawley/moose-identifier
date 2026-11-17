"""All question wording and thresholds sent to Jev live here, so they can be audited in one place.

The experiments behind these choices are summarised in SORTING_RULES.md ("Question design").
"""

import hashlib
import json

from typesafe_sdk import Choice, Noul

MODEL = "jev-1.14.0"  # pinned so thresholds don't drift when jev-latest moves
PRICE_PER_TOKEN = 0.042 / 1_000_000  # USD per input token for MODEL (output tokens are free), 2026-11-15

# A Noul above YES reads as yes, below NO reads as no; in between is unsettled.
# For the species Choice, a moose probability below NO reads as "not a moose".
YES = 0.7
NO = 0.3
# Event smoothing never promotes a photo Jev confidently calls another species: on the tuning set,
# 58 of 61 real deer scored below this, and every moose it rescued scored 0.20 or more.
SMOOTH_MIN_MOOSE = 0.1

MOOSE = "moose"  # the species option that means moose

ANIMAL = Noul(
    instructions="Is there a real, living animal in `photo`?",
    criteria={
        "true": "A real animal of any kind (mammal, bird, fish, reptile, insect, ...) is visible in the photo.",
        "false": (
            "The photo shows only people, objects, or scenery, or shows an animal only as a "
            "drawing, cartoon, toy, statue, or other depiction."
        ),
    },
)
# A Choice always picks an option, so the animal Noul is its companion for "no animal".
SPECIES = Choice(
    instructions="Which kind of animal is in `photo`?",
    criteria={
        MOOSE: "A moose, including calves, cows without antlers, and moose that are partly hidden.",
        "deer": "A white-tailed deer or mule deer.",
        "elk": "An elk.",
        "small_animal": "A smaller animal such as a raccoon, fox, bird, or rodent.",
    },
)
# Same options in reverse order; the two answers are averaged (Jev can lean to the first option).
SPECIES_REV = Choice(
    instructions=SPECIES.instructions,
    criteria=dict(reversed(list(SPECIES.criteria.items()))),
)

# Atomic, observable moose anatomy. Used only to stop a "not a moose" verdict (-> unsure) and to
# guard event smoothing; never to put a photo in the moose folder on their own.
FEATURES = {
    "f_pale_legs": Noul(
        instructions="Does the animal in `photo` have long legs that look paler than its body?",
        criteria={"true": "The lower legs are long and clearly lighter than the body, like pale stockings.",
                  "false": "The legs are about the same shade as the body, or short, or not visible."}),
    "f_hump": Noul(
        instructions="Does the animal in `photo` have a large, heavy body with a raised hump at the shoulders?",
        criteria={"true": "The body is bulky and the back rises to a hump above the front legs.",
                  "false": "The body is slim, or the back is level, or the body is not visible."}),
    "f_long_muzzle": Noul(
        instructions="Does the animal in `photo` have a long, broad muzzle that overhangs its mouth?",
        criteria={"true": "The face ends in a long, heavy, drooping nose.",
                  "false": "The face is narrow and pointed, or the muzzle is not visible."}),
}

# The question set sent with every photo (see identify.py).
SINGLE_QUESTIONS = {"animal": ANIMAL, "species": SPECIES, "species_rev": SPECIES_REV, **FEATURES}
QUESTIONS = SINGLE_QUESTIONS


def questions_fingerprint(questions=None) -> str:
    """Short hash of the model and question wording; recordings made with other wording are stale."""
    questions = SINGLE_QUESTIONS if questions is None else questions
    payload = {k: q.model_dump(mode="json") for k, q in questions.items()}
    return hashlib.sha256(json.dumps([MODEL, payload], sort_keys=True).encode()).hexdigest()[:12]

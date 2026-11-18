"""All question wording and thresholds sent to Jev live here, so they can be audited in one place.

The rules these serve, and how answers become labels, are in SORTING_RULES.md.
"""

import hashlib
import json

from typesafe_sdk import Choice, Noul

MODEL = "jev-1.14.0"  # pinned so thresholds don't drift when jev-latest moves
PRICE_PER_TOKEN = 0.042 / 1_000_000  # USD per input token for MODEL (output tokens are free), 2026-11-15

# A Noul above YES reads as yes, below NO as no; in between is unsettled. Unsettled answers never
# label a photo on their own; smoothing can confirm them from the frame next to it (identify.smooth).
YES = 0.7
NO = 0.3
# A photo can be smoothed to moose only if the species answer gave moose at least this much: on the
# old tuning set, 58 of 61 real deer scored below it, and every moose smoothing rescued scored 0.20+.
SMOOTH_MIN_MOOSE = 0.1

# The animals of SORTING_RULES.md, in its order. Each is also a label: in labels.jsonl, in the event
# folder name, and as a keyword in exported copies.
HUMAN, DOG, COUGAR, DEER, ELK, MOOSE = ANIMALS = ("human", "dog", "cougar", "deer", "elk", "moose")
NO_ANIMAL = "no_animal"        # nothing there
UNIDENTIFIED = "unidentified"  # Jev is sure there's an animal, but not which of ANIMALS it is

# The questions are asked in one request per photo, in three tiers of priority:
#
# 1. Is there an animal? Decides which photos make up events.
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
# People count as animals in SORTING_RULES.md, but ANIMAL leaves them out (it was measured that way), so
# they have their own question, which also gives the `human` label.

# 2. Is it a moose? A Choice, asked in both option orders and averaged (Jev can lean to the first
# option). Moose = an animal is there and moose is the top option. ANIMAL is its companion for "none".
SPECIES = Choice(
    instructions="Which kind of animal is in `photo`?",
    criteria={
        MOOSE: "A moose, including calves, cows without antlers, and moose that are partly hidden.",
        DEER: "A white-tailed deer or mule deer.",
        ELK: "An elk.",
        COUGAR: "A cougar (mountain lion).",
        DOG: "A domestic dog.",
        "small_animal": "A smaller animal such as a raccoon, fox, bird, or rodent.",
    },
)
SPECIES_REV = Choice(
    instructions=SPECIES.instructions,
    criteria=dict(reversed(list(SPECIES.criteria.items()))),
)

# 3. Which other animals? One Noul each, because a photo can show several (a person and a dog).
# Each `false` criterion names the animal it is most easily mistaken for.
PRESENCE: dict[str, Noul] = {
    HUMAN: Noul(
        instructions="Is there a person in `photo`?",
        criteria={"true": "A real person, or part of one such as legs or an arm, is visible.",
                  "false": "No person is visible. A person only on a sign, poster or screen doesn't count."}),
    DOG: Noul(
        instructions="Is there a dog in `photo`?",
        criteria={"true": "A domestic dog of any breed or size is visible, even if only partly.",
                  "false": "No dog is visible. A coyote, fox or wolf is not a dog."}),
    COUGAR: Noul(
        instructions="Is there a cougar in `photo`?",
        criteria={"true": "A cougar (mountain lion, puma) is visible, even if only partly.",
                  "false": "No cougar is visible. A bobcat, lynx or house cat is not a cougar."}),
    DEER: Noul(
        instructions="Is there a deer in `photo`?",
        criteria={"true": "A white-tailed deer or mule deer is visible, even if only partly.",
                  "false": "No deer is visible. An elk or a moose is not a deer."}),
    ELK: Noul(
        instructions="Is there an elk in `photo`?",
        criteria={"true": "An elk (wapiti) is visible, even if only partly.",
                  "false": "No elk is visible. A deer or a moose is not an elk."}),
}
OTHER_ANIMALS = tuple(PRESENCE)   # labelled from their own Noul; moose comes from SPECIES

# The question set sent with every photo (see identify.py).
SINGLE_QUESTIONS = {"animal": ANIMAL, "species": SPECIES, "species_rev": SPECIES_REV, **PRESENCE}
QUESTIONS = SINGLE_QUESTIONS


def questions_fingerprint(questions=None) -> str:
    """Short hash of the model and question wording.

    Cached labels (labels.jsonl) and recordings made with another fingerprint are stale.
    """
    questions = SINGLE_QUESTIONS if questions is None else questions
    payload = {k: q.model_dump(mode="json") for k, q in questions.items()}
    return hashlib.sha256(json.dumps([MODEL, payload], sort_keys=True).encode()).hexdigest()[:12]

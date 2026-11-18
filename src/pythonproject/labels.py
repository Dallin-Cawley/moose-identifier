"""Per-photo labels (which animals Jev detected) and the label store that caches them.

The answers are read in three tiers (see questions.py and SORTING_RULES.md, "Labelling"):

1. An animal is there if the `animal` Noul is above YES, or any animal below is confirmed.
2. Moose is confirmed if an animal is there and moose is the top option of the species Choice
   (both option orders averaged).
3. human, dog, cougar, deer and elk are confirmed by their own Noul above YES.

Smoothing (identify.smooth) can add an animal the photo was unsettled about. A photo with an
animal but none of the six identified is labelled UNIDENTIFIED; one with no animal, NO_ANIMAL.

The store, labels.jsonl, is the source of truth and the cache: one JSON line per photo, keyed by
the SHA-256 of the file's bytes, so it survives renames and moves. A photo whose entry was made
with the current model and question wording (questions_fingerprint) is never sent to Jev again.
"""

import dataclasses
import hashlib
import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .questions import (ANIMALS, MOOSE, NO, NO_ANIMAL, OTHER_ANIMALS, SMOOTH_MIN_MOOSE, UNIDENTIFIED,
                        YES, questions_fingerprint)

LABELS_FILE = "labels.jsonl"   # default location: inside the output folder


@dataclass(frozen=True)
class Labels:
    path: Path
    # "animal": P(an animal is there); "species": averaged species Choice {option: P};
    # human/dog/cougar/deer/elk: P(present) from their own Noul
    probabilities: dict
    model: str
    input_tokens: int | None = None   # None when the answers came from the store
    cached: bool = False
    smoothed: tuple[str, ...] = ()    # animals added by smoothing (unsettled here, confirmed next door)

    @property
    def species_top(self) -> str:
        sp = self.probabilities["species"]
        return max(sp, key=sp.get)

    @property
    def moose(self) -> float:
        """P(species is moose), averaged over both option orders."""
        return self.probabilities["species"][MOOSE]

    @property
    def animal_sure(self) -> bool:
        return self.probabilities["animal"] > YES

    @property
    def confirmed(self) -> tuple[str, ...]:
        """Animals identified in this photo on its own, in SORTING_RULES.md order."""
        found = {a for a in OTHER_ANIMALS if self.probabilities[a] > YES}
        if self.animal_sure and self.species_top == MOOSE:
            found.add(MOOSE)
        return tuple(a for a in ANIMALS if a in found)

    @property
    def unsettled(self) -> tuple[str, ...]:
        """Animals that may be there; smoothing can confirm them from a neighbouring frame.

        Others: NO <= P <= YES. Moose: an animal is surely there and moose isn't the top option,
        but Jev didn't rule it out (P(moose) >= SMOOTH_MIN_MOOSE).
        """
        maybe = {a for a in OTHER_ANIMALS if NO <= self.probabilities[a] <= YES}
        if self.animal_sure and self.species_top != MOOSE and self.moose >= SMOOTH_MIN_MOOSE:
            maybe.add(MOOSE)
        return tuple(a for a in ANIMALS if a in maybe)

    @property
    def animals(self) -> tuple[str, ...]:
        """The identified animals: confirmed plus smoothed, in SORTING_RULES.md order."""
        return tuple(a for a in ANIMALS if a in self.confirmed or a in self.smoothed)

    @property
    def has_animal(self) -> bool:
        """Tier 1: does this photo count as animal activity (and so belong to an event)?"""
        return self.animal_sure or bool(self.animals)

    @property
    def keywords(self) -> tuple[str, ...]:
        if self.animals:
            return self.animals
        return (UNIDENTIFIED,) if self.has_animal else (NO_ANIMAL,)


def read_answers(answers) -> dict:
    """The probabilities Labels reads, from a response's answers."""
    orders = [answers[k].probabilities for k in ("species", "species_rev")]
    species = {opt: sum(o[opt] for o in orders) / len(orders) for opt in orders[0]}
    return {"animal": answers["animal"].noul, "species": species,
            **{a: answers[a].noul for a in OTHER_ANIMALS}}


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class LabelStore:
    """labels.jsonl: the label cache. The last line for a SHA-256 wins.

    Entries are appended as answers arrive, so an interrupted run keeps what it paid for, and
    `save` rewrites the file once at the end with each photo's final labels (after smoothing).
    """

    def __init__(self, path: Path, writable: bool = True):
        self.path = path
        self.writable = writable
        self.fingerprint = questions_fingerprint()
        self._by_sha: dict[str, dict] = {}
        self._sha_of: dict[tuple[str, int, int], str] = {}   # (path, size, mtime_ns) -> sha256
        if path.is_file():
            for line in path.read_text().splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._by_sha[entry["sha256"]] = entry
                    self._sha_of[(entry["path"], entry["size"], entry["mtime_ns"])] = entry["sha256"]

    def __len__(self) -> int:
        return len(self._by_sha)

    def sha256(self, photo: Path) -> str:
        """Content hash of the photo, reusing the stored one when path, size and mtime are unchanged."""
        st = photo.stat()
        key = (str(photo.resolve()), st.st_size, st.st_mtime_ns)
        if key not in self._sha_of:
            self._sha_of[key] = file_sha256(photo)
        return self._sha_of[key]

    def cached(self, photo: Path) -> Labels | None:
        """The stored answers for this photo, if made with the current model and questions."""
        entry = self._by_sha.get(self.sha256(photo))
        if entry is None or entry["questions"] != self.fingerprint:
            return None
        return Labels(photo, dict(entry["probabilities"]), entry["model"], None, cached=True)

    def keywords(self, photo: Path) -> tuple[str, ...] | None:
        """The photo's final labels from the last run, whatever wording made them; None if unknown."""
        entry = self._by_sha.get(self.sha256(photo))
        return tuple(entry["labels"]) if entry else None

    def _entry(self, labels: Labels) -> dict:
        st = labels.path.stat()
        return {
            "sha256": self.sha256(labels.path), "path": str(labels.path.resolve()),
            "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "model": labels.model, "questions": self.fingerprint,
            "probabilities": labels.probabilities, "labels": list(labels.keywords),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def record(self, labels: Labels) -> None:
        """Store freshly paid-for answers right away."""
        entry = self._entry(labels)
        self._by_sha[entry["sha256"]] = entry
        if self.writable:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(entry) + "\n")

    def save(self, final: Iterable[Labels]) -> None:
        """Update each photo's final labels and rewrite the file compactly (one line per photo)."""
        for labels in final:
            sha = self.sha256(labels.path)
            if sha in self._by_sha and self._by_sha[sha]["questions"] == self.fingerprint:
                self._by_sha[sha] = {**self._by_sha[sha], "labels": list(labels.keywords)}
        if not self.writable:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".partial")
        with open(tmp, "w") as f:
            for entry in self._by_sha.values():
                f.write(json.dumps(entry) + "\n")
        os.replace(tmp, self.path)


def with_smoothed(labels: Labels, animals: Iterable[str]) -> Labels:
    return dataclasses.replace(labels, smoothed=tuple(a for a in ANIMALS if a in set(animals)))

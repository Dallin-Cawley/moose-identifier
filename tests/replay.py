"""Replay recorded Jev answers through the production code. No API calls.

    uv run python tests/replay.py          # print the accuracy table for every recorded run

Recordings are made by scripts/experiment.py (arm `labels`, the production questions) and saved in
tests/fixtures/experiments/<set>__labels__run<n>.json with each photo's capture time.

The labelled sets predate the six-animal rules: each photo is labelled animal/moose,
animal/not_moose, no_animal or ambiguous, and no_animal includes photos of people. So the scores
here cover tier 1 (is an animal detected?) and tier 2 (is a moose labelled moose?), plus a check
that empty frames don't get a (non-human) animal. Tier 3 (dog, cougar, elk, human) can't be scored
with these labels.
"""

import csv
import json
from datetime import datetime
from pathlib import Path

from typesafe_sdk import ChoiceAnswer, NoulAnswer

from pythonproject.identify import labels_from_answers, smooth
from pythonproject.labels import Labels
from pythonproject.questions import HUMAN, MOOSE, questions_fingerprint

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
EXP = FIX / "experiments"
LABELS = {"dev": FIX / "dev_labels.csv", "test": FIX / "test_labels.csv"}
ARM = "labels"


def labels(set_name: str) -> dict[str, str]:
    return {r["photo"]: r["expected"] for r in csv.DictReader(open(LABELS[set_name]))}


def recording(set_name: str, run: int = 1) -> dict | None:
    """Raw recording, or None if absent or made with other question wording."""
    f = EXP / f"{set_name}__{ARM}__run{run}.json"
    if not f.exists():
        return None
    rec = json.loads(f.read_text())
    if any(r.get("questions") != questions_fingerprint() for r in rec.values()):
        return None
    return rec


def results(set_name: str, run: int = 1, with_context: bool = True, smoothing: bool = True
            ) -> dict[Path, Labels] | None:
    """Production Labels for every recorded photo (plus unlabelled neighbours, for smoothing)."""
    rec = recording(set_name, run)
    if rec is None:
        return None
    if with_context and (ctx := recording(f"{set_name}nb", 1)):
        rec = {**ctx, **rec}
    model = {"noul": NoulAnswer, "choice": ChoiceAnswer}
    out = {Path(p): labels_from_answers(Path(p), {k: model[a["type"]].model_validate(a)
                                                  for k, a in r["answers"].items()},
                                        r["model"], r["input_tokens"])
           for p, r in rec.items()}
    if smoothing:
        timeline = sorted(((Path(p), datetime.fromisoformat(r["taken"])) for p, r in rec.items()),
                          key=lambda pt: pt[1])
        out = smooth(out, timeline)
    return out


def metrics(set_name: str, rs: dict[Path, Labels]) -> dict:
    """Scores per tier. Tier 1: is an animal detected? Tier 2: is a moose labelled moose?"""
    lab = labels(set_name)
    pairs = [(e, rs[Path(p)]) for p, e in lab.items() if e != "ambiguous" and Path(p) in rs]
    moose = [r for e, r in pairs if e == "animal/moose"]
    others = [r for e, r in pairs if e == "animal/not_moose"]
    empty = [r for e, r in pairs if e == "no_animal"]          # includes photos of people
    return {
        "n": len(pairs),
        # tier 1
        "animal_found": sum(r.has_animal for r in moose + others),
        "animal_total": len(moose + others),
        "empty_given_animal": sum(r.has_animal and set(r.keywords) != {HUMAN} for r in empty),
        # tier 2
        "moose_total": len(moose),
        "moose_found": sum(MOOSE in r.animals for r in moose),
        "moose_missed": sum(MOOSE not in r.animals for r in moose),
        "moose_dropped": sum(not r.has_animal for r in moose),               # left out of every event
        "moose_unidentified": sum(r.keywords == ("unidentified",) for r in moose),  # kept, unnamed
        "moose_as_other": sum(MOOSE not in r.animals and bool(r.animals) for r in moose),
        "false_moose": sum(MOOSE in r.animals for r in others + empty),
        "tokens_per_photo": round(sum(rs[Path(p)].input_tokens or 0 for p in lab if Path(p) in rs)
                                  / max(1, sum(Path(p) in rs for p in lab))),
    }


if __name__ == "__main__":
    found = False
    for set_name in LABELS:
        for run in (1, 2, 3):
            for smoothing in (False, True):
                rs = results(set_name, run, smoothing=smoothing)
                if rs is None:
                    continue
                found = True
                m = metrics(set_name, rs)
                print(f"{set_name:<4} run{run} {'smoothed' if smoothing else 'raw':<8} "
                      f"animal found {m['animal_found']:>3}/{m['animal_total']:<3} "
                      f"empty→animal {m['empty_given_animal']} | "
                      f"moose found {m['moose_found']:>3}/{m['moose_total']:<3} missed {m['moose_missed']:>2} "
                      f"(dropped {m['moose_dropped']}, kept unidentified {m['moose_unidentified']}, "
                      f"named other {m['moose_as_other']}) false-moose {m['false_moose']} "
                      f"tokens/photo {m['tokens_per_photo']:,}")
    if not found:
        print("no recordings with the current questions yet: run scripts/experiment.py dev (and devnb)")

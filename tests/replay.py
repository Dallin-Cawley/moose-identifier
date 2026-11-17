"""Replay recorded Jev answers through the production code. No API calls.

    uv run python tests/replay.py          # print the accuracy table for every recorded configuration

Recordings are raw answers saved by scripts/experiment.py in tests/fixtures/experiments/.
Arm B_nobanner asked exactly the production questions (checked by test_recorded.py against
questions_used.json), on banner-cropped photos. The dropped `neighbours` experiment (arm E_nb2) can
still be scored with scripts/score.py.
"""

import csv
import json
from functools import cache
from pathlib import Path

from typesafe_sdk import ChoiceAnswer, NoulAnswer

from pythonproject import find_images, group_into_events
from pythonproject.classify import Category, Result
from pythonproject.events import StaticWindows
from pythonproject.identify import find_neighbours, flag_for_review, result_from_answers, smooth

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
EXP = FIX / "experiments"
IMAGES = ROOT / "src" / "images"
LABELS = {"dev": FIX / "dev_labels.csv", "test": FIX / "test_labels.csv"}
# production strategy -> (recorded arm, which recorded question plays production's `animal`)
ARMS = {"single": ("B_nobanner", "animal")}


def labels(set_name: str) -> dict[str, str]:
    return {r["photo"]: r["expected"] for r in csv.DictReader(open(LABELS[set_name]))}


def recording(set_name: str, arm: str, run: int = 1) -> dict | None:
    f = EXP / f"{set_name}__{arm}__run{run}.json"
    return json.loads(f.read_text()) if f.exists() else None


def _answers(raw: dict, animal_q: str) -> dict:
    out = {}
    for qid, a in raw.items():
        model = {"noul": NoulAnswer, "choice": ChoiceAnswer}.get(a["type"])
        if model:
            out[qid] = model.model_validate(a)
    out["animal"] = out[animal_q]
    return out


def results(set_name: str, strategy: str, run: int = 1, with_context: bool = False) -> dict[Path, Result] | None:
    """Production Results for every recorded photo (and, with_context, its unlabelled neighbours)."""
    arm, animal_q = ARMS[strategy]
    rec = recording(set_name, arm, run)
    if rec is None:
        return None
    if with_context:
        ctx = recording(f"{set_name}nb", arm, 1)
        if ctx is None:
            return None
        rec = {**ctx, **rec}
    return {Path(p): result_from_answers(Path(p), _answers(r["answers"], animal_q), r["model"], r["input_tokens"])
            for p, r in rec.items()}


@cache
def card_photos():
    photos, _ = group_into_events(find_images([IMAGES], Path("/nonexistent")), StaticWindows())
    return photos


@cache
def card_neighbours():
    return {Path(p.name): v.__class__(tuple(Path(q.name) for q in v.before), tuple(Path(q.name) for q in v.after))
            for p, v in find_neighbours(card_photos(), 1).items()}


@cache
def card_events():
    return {Path(p.name): e for p, e in card_photos()}


def smoothed(rs: dict[Path, Result]) -> dict[Path, Result]:
    return smooth(rs, card_neighbours())


def reviewed(rs: dict[Path, Result]) -> dict[Path, Result]:
    """The check-folder pass. With replays, an event's moose are only seen among recorded photos."""
    return flag_for_review(rs, card_events())


def metrics(set_name: str, rs: dict[Path, Result]) -> dict:
    lab = labels(set_name)
    pairs = [(e, rs[Path(p)].category.value) for p, e in lab.items() if e != "ambiguous" and Path(p) in rs]
    moose = [g for e, g in pairs if e == "animal/moose"]
    check = "animal/not_moose_check"
    return {
        "n": len(pairs),
        # a real deer in the check folder counts as correct: it is still filed as "not a moose"
        "correct": sum(e == g or (e == "animal/not_moose" and g == check) for e, g in pairs),
        "check_folder": sum(g == check for _, g in pairs),
        "check_moose": sum(g == check for g in moose),
        "moose_total": len(moose),
        "moose_found": sum(g == "animal/moose" for g in moose),
        "hard_miss": sum(g in ("animal/not_moose", "no_animal") for g in moose),
        "moose_unsure": sum(g == "unsure" for g in moose),
        "false_moose": sum(e != "animal/moose" and g == "animal/moose" for e, g in pairs),
        "people_to_animal": sum(e == "no_animal" and g.startswith("animal/") for e, g in pairs),
        "unsure": sum(g == "unsure" for _, g in pairs),
        "wrong_folder": sum(g not in (e, "unsure", check) for e, g in pairs),
        "tokens_per_photo": round(sum(r.input_tokens for p, r in rs.items() if p.name in lab) / max(1, sum(p.name in lab for p in rs))),
    }


def configurations(set_name: str, run: int = 1):
    """(name, Results) for each configuration that has recordings."""
    single = results(set_name, "single", run, with_context=True) or results(set_name, "single", run)
    if single:
        yield "single, no smoothing", reviewed(single)
        if recording(f"{set_name}nb", "B_nobanner") and run == 1:
            yield "single + smoothing", reviewed(smoothed(single))


if __name__ == "__main__":
    for set_name in LABELS:
        for run in (1, 2):
            for name, rs in configurations(set_name, run):
                m = metrics(set_name, rs)
                print(f"{set_name:<4} run{run}  {name:<26} correct {m['correct']:>3}/{m['n']:<3} "
                      f"moose found {m['moose_found']:>3}/{m['moose_total']:<3} hard-miss {m['hard_miss']:>2} "
                      f"moose→unsure {m['moose_unsure']:>2} moose→check {m['check_moose']} "
                      f"check folder {m['check_folder']:>2} false-moose {m['false_moose']} "
                      f"people→animal {m['people_to_animal']} unsure {m['unsure']:>3} "
                      f"tokens/photo {m['tokens_per_photo']:,}")

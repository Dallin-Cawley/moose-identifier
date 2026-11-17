"""Score recorded experiment answers against labels. Free: no API calls.

    uv run python scripts/score.py dev A_plain [B_nobanner ...]   # all readers for each arm
    uv run python scripts/score.py dev A_plain --misses            # list the photos each reader gets wrong

A "reader" turns one photo's raw answers into a folder. Several readers can score the same
recorded answers (e.g. option order A, order B, their average), so variants cost nothing extra.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from pythonproject.classify import Category, decide  # noqa: E402
from pythonproject.questions import MOOSE  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
EXP = FIX / "experiments"
SETS = {"dev": FIX / "dev_labels.csv", "test": FIX / "test_labels.csv"}


def probs(a):
    return a["probabilities"]


def avg(*answers):
    keys = probs(answers[0]).keys()
    return {k: sum(probs(a)[k] for a in answers) / len(answers) for k in keys}


def from_probs(animal, p):
    top = max(p, key=p.get)
    return decide(animal, p[MOOSE], top)


def choice_reader(*qids):
    """Folder from the `animal` Noul and the average of one or more species Choices."""
    def read(ans, animal):
        return from_probs(animal, avg(*(ans[q] for q in qids)))
    return read


def moose_features(ans):
    """Observable moose anatomy: a shoulder hump plus pale long legs or a long overhanging muzzle."""
    f = lambda q: ans[q]["noul"]
    return f("f_hump") > 0.5 and (f("f_pale_legs") > 0.7 or f("f_long_muzzle") > 0.5)


def rescue(base):
    """Never claim 'not a moose' when moose anatomy is visible: send it to `unsure` instead."""
    def read(ans, animal):
        got = base(ans, animal)
        if got == Category.OTHER_ANIMAL and moose_features(ans):
            return Category.UNSURE
        return got
    return read


READERS = {
    "species": choice_reader("species"),
    "species_rev": choice_reader("species_rev"),
    "species_avg": choice_reader("species", "species_rev"),
    "species+rescue": rescue(choice_reader("species")),
    "avg+rescue": rescue(choice_reader("species", "species_rev")),
    "seq": choice_reader("species_seq"),
    "seq_avg": choice_reader("species_seq", "species_rev"),
    "seq+rescue": rescue(choice_reader("species_seq")),
    "close": choice_reader("species_close"),
    "close_avg": choice_reader("species_close", "species_close_rev"),
}


def load(set_name, arm, run=1):
    return json.loads((EXP / f"{set_name}__{arm}__run{run}.json").read_text())


def labels(set_name):
    return {r["photo"]: r for r in csv.DictReader(open(SETS[set_name]))}


def score(set_name, results, reader, animal_from=None, animal_q="animal"):
    """Returns metrics dict and per-photo (expected, got)."""
    lab = labels(set_name)
    out = {}
    for photo, r in results.items():
        exp = lab[photo]["expected"]
        if exp == "ambiguous":
            continue
        ans = r["answers"]
        if animal_q in ans:
            animal = ans[animal_q]["noul"]
        else:
            animal = animal_from[photo]["answers"]["animal"]["noul"]
        try:
            got = reader(ans, animal).value
        except KeyError:
            return None, None
        out[photo] = (exp, got)
    # photos the arm skipped (zoom skips "no animal"): fall back to the animal-only reading
    if animal_from:
        for photo, r in animal_from.items():
            exp = lab[photo]["expected"]
            if photo not in out and exp != "ambiguous":
                out[photo] = (exp, decide(r["answers"]["animal"]["noul"], 0.0, "deer").value)
    m = {
        "n": len(out),
        "correct": sum(e == g for e, g in out.values()),
        "hard_miss": sum(e == "animal/moose" and g in ("animal/not_moose", "no_animal") for e, g in out.values()),
        "moose_unsure": sum(e == "animal/moose" and g == "unsure" for e, g in out.values()),
        "moose_found": sum(e == g == "animal/moose" for e, g in out.values()),
        "moose_total": sum(e == "animal/moose" for e, _ in out.values()),
        "false_moose": sum(e != "animal/moose" and g == "animal/moose" for e, g in out.values()),
        "people_leak": sum(e == "no_animal" and g.startswith("animal/") for e, g in out.values()),
        "unsure": sum(g == "unsure" for _, g in out.values()),
        "wrong_folder": sum(g not in (e, "unsure") for e, g in out.values()),
    }
    return m, out


def fmt(m):
    return (f"correct {m['correct']:>3}/{m['n']:<3} | moose found {m['moose_found']:>3}/{m['moose_total']:<3} "
            f"hard-miss {m['hard_miss']:>2}  moose→unsure {m['moose_unsure']:>2} | false-moose {m['false_moose']:>2} "
            f"people→animal {m['people_leak']:>2} | unsure {m['unsure']:>3}  wrong {m['wrong_folder']:>2}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set", choices=SETS)
    ap.add_argument("arms", nargs="+")
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--misses", action="store_true")
    ap.add_argument("--animal-from", default="A_plain", help="arm that supplies `animal` when an arm lacks it")
    args = ap.parse_args()
    for arm in args.arms:
        res = load(args.set, arm, args.run)
        animal_from = load(args.set, args.animal_from, args.run) if arm == "F_zoom" else None
        tok = sum(r["input_tokens"] for r in res.values())
        print(f"\n== {args.set} / {arm} / run{args.run}  ({len(res)} photos, {tok/len(res):,.0f} tokens/photo)")
        animal_qs = ["animal"] + (["animal_self"] if "animal_self" in next(iter(res.values()))["answers"] else [])
        for aq in animal_qs:
          for name, reader in READERS.items():
            m, out = score(args.set, res, reader, animal_from, aq)
            if m is None:
                continue
            print(f"  {(name if aq == 'animal' else name + '/self'):<15} {fmt(m)}")
            if args.misses:
                lab = labels(args.set)
                for p, (e, g) in sorted(out.items()):
                    if g != e:
                        print(f"      {p} expected {e:<16} got {g:<16} ({lab[p]['note']})")


if __name__ == "__main__":
    main()

"""Record Jev's answers to the production questions over a labelled set. CALLS THE LIVE API (budget-guarded).

    uv run python scripts/experiment.py dev --estimate   # projected cost only
    uv run python scripts/experiment.py dev              # record (resumes if interrupted)
    uv run python scripts/experiment.py dev --run 2      # a repeat run (variance)

Raw answers go to tests/fixtures/experiments/<set>__labels__run<n>.json, with each photo's capture
time, so tests/replay.py can score them (and smooth them) without the photo card. The exact
questions are saved under "labels" in questions_used.json; tests/test_recorded.py refuses to score
recordings made with other wording. Spend is logged to .typesafe_spend.jsonl and capped
(pythonproject.budget).

The earlier arms (A_plain ... F_zoom) asked the old moose/not-moose questions; their recordings stay
in tests/fixtures/experiments/ as history but can't be replayed through the current code.
"""

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient

from pythonproject import DEFAULT_KEY_FILE, load_api_key
from pythonproject.budget import PRICE_PER_TOKEN, TOKENS_PER_IMAGE, BudgetExceeded, GuardedClient
from pythonproject.events import read_timestamp
from pythonproject.identify import SinglePhoto
from pythonproject.questions import MODEL, SINGLE_QUESTIONS, questions_fingerprint

ROOT = Path(__file__).resolve().parent.parent
PHOTOS = ROOT / "src" / "images" / "12.11.4.2024"
FIX = ROOT / "tests" / "fixtures"
OUT = FIX / "experiments"
ARM = "labels"
SETS = {"dev": FIX / "dev_labels.csv", "test": FIX / "test_labels.csv",
        "devnb": OUT / "devnb_photos.csv",    # unlabelled neighbours of dev photos (for smoothing)
        "testnb": OUT / "testnb_photos.csv"}  # unlabelled neighbours of test photos


def save_questions_used() -> None:
    f = OUT / "questions_used.json"
    used = json.loads(f.read_text()) if f.exists() else {}
    used[ARM] = {k: q.model_dump(mode="json") for k, q in SINGLE_QUESTIONS.items()}
    used[ARM + "_fingerprint"] = questions_fingerprint()
    f.write_text(json.dumps(used, indent=1) + "\n")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set", choices=SETS)
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--estimate", action="store_true")
    args = ap.parse_args()

    names = [r["photo"] for r in csv.DictReader(open(SETS[args.set]))]
    out_file = OUT / f"{args.set}__{ARM}__run{args.run}.json"
    results = json.loads(out_file.read_text()) if out_file.exists() else {}
    if results and next(iter(results.values())).get("questions") != questions_fingerprint():
        sys.exit(f"{out_file.name} was recorded with other question wording; use another --run number")
    todo = [n for n in names if n not in results]   # resume
    est = len(todo) * TOKENS_PER_IMAGE * PRICE_PER_TOKEN
    print(f"{args.set}/{ARM}: {len(todo)} of {len(names)} photos to send, estimated ${est:.3f}", file=sys.stderr)
    if args.estimate or not todo:
        return

    OUT.mkdir(parents=True, exist_ok=True)
    save_questions_used()
    strategy = SinglePhoto()
    sem = asyncio.Semaphore(8)
    async with AsyncTypeSafeClient(api_key=load_api_key(DEFAULT_KEY_FILE),
                                   headers={"TypeSafe-Danger-Zone-Images": "true"}) as raw:
        client = GuardedClient(raw, "experiment")

        async def one(name):
            photo = PHOTOS / name
            state, questions = strategy.build(photo)
            async with sem:
                r = await client.system_one(state=state, questions=questions, model=MODEL,
                                            arm=f"{args.set}:{ARM}", photo=name, images=1)
            results[name] = {"answers": {k: v.model_dump() for k, v in r.answers.items()},
                             "input_tokens": r.usage.input_tokens, "model": r.model,
                             "questions": questions_fingerprint(),
                             "taken": read_timestamp(photo).isoformat()}

        outcome = await asyncio.gather(*(one(n) for n in todo), return_exceptions=True)
    errors = [(n, o) for n, o in zip(todo, outcome) if isinstance(o, BaseException)]
    for name, e in errors[:5]:
        print(f"ERROR {name}: {type(e).__name__}: {e}", file=sys.stderr)
    out_file.write_text(json.dumps(dict(sorted(results.items())), indent=1) + "\n")
    tok = sum(r["input_tokens"] for r in results.values())
    print(f"wrote {out_file.name}: {len(results)} photos, {len(errors)} errors, {tok:,} tokens "
          f"(${tok * PRICE_PER_TOKEN:.3f}); total spent ${client.spent:.3f}", file=sys.stderr)
    if any(isinstance(e, BudgetExceeded) for _, e in errors):
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())

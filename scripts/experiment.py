"""Run one experiment arm over a labelled set. CALLS THE LIVE API (budget-guarded).

    uv run python scripts/experiment.py dev A_plain            # run an arm
    uv run python scripts/experiment.py dev A_plain --estimate # projected cost only
    uv run python scripts/experiment.py dev A_plain --run 2    # a repeat run (variance)

Raw answers go to tests/fixtures/experiments/<set>__<arm>__run<n>.json; score them with
scripts/score.py. Every variant inside an arm rides in the same request, so variants are compared
on identical input. Spend is logged to .typesafe_spend.jsonl and capped (pythonproject.budget).
"""

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, Box, Choice, Image, Noul

from pythonproject import DEFAULT_KEY_FILE, find_images, group_into_events, load_api_key
from pythonproject.budget import TOKENS_PER_IMAGE, PRICE_PER_TOKEN, BudgetExceeded, GuardedClient
from pythonproject.classify import prepare_image
from pythonproject.events import StaticWindows, read_timestamp
from pythonproject.identify import find_neighbours
from pythonproject.questions import ANIMAL, FEATURES as PROD_FEATURES, MODEL, SPECIES, SPECIES_REV

ROOT = Path(__file__).resolve().parent.parent
PHOTOS = ROOT / "src" / "images" / "12.11.4.2024"
FIX = ROOT / "tests" / "fixtures"
OUT = FIX / "experiments"
SETS = {"dev": FIX / "dev_labels.csv", "test": FIX / "test_labels.csv",
        "devnb": OUT / "devnb_photos.csv",    # unlabelled neighbours of dev photos (for smoothing)
        "testnb": OUT / "testnb_photos.csv"}  # unlabelled neighbours of test photos

# Production definitions (questions.py) plus experiment-only extras, so arms can't drift from production.
FEATURES = {  # E2: atomic, observable back-view features
    "f_pale_legs": PROD_FEATURES["f_pale_legs"],
    "f_white_rump": Noul(  # dropped: moose rumps look pale in infrared too
        instructions="Does the animal in `photo` show a white patch on its rump or under its tail?",
        criteria={"true": "A distinct white patch is visible on the rump or the underside of the tail.",
                  "false": "The rump and tail are the same shade as the body, or not visible."}),
    "f_hump": PROD_FEATURES["f_hump"],
    "f_head_visible": Noul(  # dropped: not needed by the final rules
        instructions="Is the head of the animal in `photo` visible?",
        criteria={"true": "Most of the head (face, ears or muzzle) can be seen.",
                  "false": "The head is hidden behind something, turned away, or out of the frame."}),
    "f_long_muzzle": PROD_FEATURES["f_long_muzzle"],
}
# E5 (neighbours, dropped): with neighbouring photos in the state, the plain animal question leaked
# (an animal in `earlier`/`later` raised "animal" on empty frames); naming the case fixed most of it.
ANIMAL_SELF = Noul(
    instructions="Is there a real, living animal in `photo` itself?",
    criteria={"true": "A real animal is visible in `photo`, even if only partly.",
              "false": ("`photo` shows no animal, even if an animal is visible in `earlier` or `later`; "
                        "people and empty scenery don't count.")})
WHERE = Box(instructions="the animal in `photo`")  # for the zoom arm


def single_questions():
    return {"animal": ANIMAL, "species": SPECIES, "species_rev": SPECIES_REV, **FEATURES, "where": WHERE}


def neighbour_questions():
    seq = Choice(
        instructions=("Which kind of animal is in `photo`? `earlier` and `later` are photos of the same "
                      "spot taken seconds before and after it."),
        criteria=SPECIES.criteria)
    return {"animal": ANIMAL, "animal_self": ANIMAL_SELF, "species": SPECIES,
            "species_rev": SPECIES_REV, "species_seq": seq, **FEATURES}


def zoom_questions():
    close = Choice(instructions="Which kind of animal is in `closeup`?", criteria=SPECIES.criteria)
    close_rev = Choice(instructions="Which kind of animal is in `closeup`?", criteria=SPECIES_REV.criteria)
    return {"species": SPECIES, "species_close": close, "species_close_rev": close_rev}


def neighbour_state(photo, nb, prep, taken):
    t0 = taken[photo]
    state = {"photo": Image(prep(photo))}
    if nb.before:
        state["earlier"] = [{"seconds_before": int((t0 - taken[p]).total_seconds()), "image": Image(prep(p))}
                            for p in nb.before]
    if nb.after:
        state["later"] = [{"seconds_after": int((taken[p] - t0).total_seconds()), "image": Image(prep(p))}
                          for p in nb.after]
    return state, 1 + len(nb.before) + len(nb.after)


def pad_box(b, pad=0.15, min_size=0.3):
    l, t, r, bt = b["left"], b["top"], b["right"], b["bottom"]
    w, h = r - l, bt - t
    l, r = l - pad * w, r + pad * w
    t, bt = t - pad * h, bt + pad * h
    if r - l < min_size:
        c = (l + r) / 2; l, r = c - min_size / 2, c + min_size / 2
    if bt - t < min_size:
        c = (t + bt) / 2; t, bt = c - min_size / 2, c + min_size / 2
    return (max(0.0, l), max(0.0, t), min(1.0, r), min(1.0, bt))


def build_arms(ctx):
    plain = lambda p: prepare_image(p)
    nobanner = lambda p: prepare_image(p, crop_banner=True)
    enhanced = lambda p: prepare_image(p, crop_banner=True, enhance_ir=True)
    arms = {
        # E1 + E2 + Box, three image preparations (E3)
        "A_plain":    lambda p: ({"photo": Image(plain(p))}, single_questions(), 1),
        "B_nobanner": lambda p: ({"photo": Image(nobanner(p))}, single_questions(), 1),
        "C_enhanced": lambda p: ({"photo": Image(enhanced(p))}, single_questions(), 1),
    }

    def neighbours(k):  # E5: neighbours from the same event
        def arm(p):
            state, images = neighbour_state(p, ctx[f"nb{k}"][p], ctx["prep"], ctx["taken"])
            return state, neighbour_questions(), images
        return arm
    arms["D_nb1"], arms["E_nb2"] = neighbours(1), neighbours(2)

    def zoom(p):  # E4: second request on a crop around the Box from arm A
        a = ctx["boxes"].get(p.name)
        if a is None:
            return None  # no animal per arm A: production would skip the second request too
        box = pad_box(a)
        state = {"photo": Image(ctx["prep"](p)),
                 "closeup": Image(prepare_image(p, crop_banner=ctx["banner"], box=box))}
        return state, zoom_questions(), 2
    arms["F_zoom"] = zoom
    return arms


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set", choices=SETS)
    ap.add_argument("arm")
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--prep", choices=["plain", "nobanner", "enhanced"], default="plain",
                    help="image preparation for neighbour/zoom arms")
    ap.add_argument("--box-from", default="A_plain", help="arm whose `where` boxes the zoom arm uses")
    args = ap.parse_args()

    labels = [r["photo"] for r in csv.DictReader(open(SETS[args.set]))]
    ctx = {"prep": {"plain": lambda p: prepare_image(p),
                    "nobanner": lambda p: prepare_image(p, crop_banner=True),
                    "enhanced": lambda p: prepare_image(p, crop_banner=True, enhance_ir=True)}[args.prep],
           "banner": args.prep != "plain"}
    if args.arm in ("D_nb1", "E_nb2"):
        photos, _ = group_into_events(find_images([PHOTOS], Path("/nonexistent")), StaticWindows())
        ctx["nb1"], ctx["nb2"] = find_neighbours(photos, 1), find_neighbours(photos, 2)
        ctx["taken"] = {p: read_timestamp(p) for p, _ in photos}
    if args.arm == "F_zoom":
        src = json.loads((OUT / f"{args.set}__{args.box_from}__run1.json").read_text())
        ctx["boxes"] = {n: r["answers"]["where"]["box"] for n, r in src.items()
                        if r["answers"]["animal"]["noul"] >= 0.3}
    arm = build_arms(ctx)[args.arm]

    jobs = []
    for name in labels:
        built = arm(PHOTOS / name)
        if built is not None:
            jobs.append((name, *built))
    n_images = sum(j[3] for j in jobs)
    est = n_images * TOKENS_PER_IMAGE * PRICE_PER_TOKEN
    print(f"{args.set}/{args.arm}: {len(jobs)} requests, {n_images} images, estimated ${est:.3f}", file=sys.stderr)
    if args.estimate:
        return

    out_file = OUT / f"{args.set}__{args.arm}__run{args.run}.json"
    OUT.mkdir(parents=True, exist_ok=True)
    results = json.loads(out_file.read_text()) if out_file.exists() else {}
    jobs = [j for j in jobs if j[0] not in results]  # resume
    sem = asyncio.Semaphore(8)
    async with AsyncTypeSafeClient(api_key=load_api_key(DEFAULT_KEY_FILE),
                                   headers={"TypeSafe-Danger-Zone-Images": "true"}) as raw:
        client = GuardedClient(raw, "experiment")

        async def one(name, state, questions, images):
            async with sem:
                r = await client.system_one(state=state, questions=questions, model=MODEL,
                                            arm=f"{args.set}:{args.arm}", photo=name, images=images)
            results[name] = {"answers": {k: v.model_dump() for k, v in r.answers.items()},
                             "input_tokens": r.usage.input_tokens, "images": images, "model": r.model}

        outcome = await asyncio.gather(*(one(*j) for j in jobs), return_exceptions=True)
    errors = [(j[0], o) for j, o in zip(jobs, outcome) if isinstance(o, BaseException)]
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

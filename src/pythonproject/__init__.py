"""moose-sort: classify images with Jev and symlink them into event/category folders.

See SORTING_RULES.md for the event and classification rules.
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient

from .classify import Category, Result
from .identify import (SMOOTH_NEIGHBOURS, TOKENS_PER_IMAGE, IdentificationStrategy, SinglePhoto,
                       find_neighbours, flag_for_review, smooth)
from .events import STRATEGIES, Event, EventStrategy, read_timestamp
from .questions import PRICE_PER_TOKEN

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
DEFAULT_KEY_FILE = Path(__file__).resolve().parent.parent / "api_key"  # src/api_key


def load_api_key(key_file: Path) -> str | None:
    """Key from key_file, else TYPESAFE_API_KEY from the environment.

    The file shipped in the repo holds a placeholder like "<put your TypeSafe API key here ...>";
    a missing, empty or placeholder file counts as "no key" and falls back to the environment.
    """
    if key_file.is_file():
        key = key_file.read_text().strip()
        if key and not key.startswith("<"):
            return key
    return os.environ.get("TYPESAFE_API_KEY") or None


def find_images(inputs: list[Path], exclude: Path) -> list[Path]:
    found: dict[Path, None] = {}
    for item in inputs:
        item = item.resolve()
        if item.is_file():
            if item.suffix.lower() in IMAGE_SUFFIXES:
                found[item] = None
            else:
                print(f"skipping non-image file: {item}", file=sys.stderr)
        elif item.is_dir():
            for root, dirs, files in os.walk(item):  # does not follow symlinked dirs
                root_path = Path(root)
                dirs[:] = [d for d in dirs if (root_path / d).resolve() != exclude]
                for name in files:
                    p = root_path / name
                    if p.suffix.lower() in IMAGE_SUFFIXES and not p.is_symlink():
                        found[p] = None
        else:
            print(f"not found: {item}", file=sys.stderr)
    return sorted(found)


def group_into_events(
    images: list[Path], strategy: EventStrategy
) -> tuple[list[tuple[Path, Event]], list[Path]]:
    """Read capture times and assign events. Returns (photos in time order, photos without a timestamp)."""
    stamped, missing = [], []
    for path in images:
        try:
            stamped.append((path, read_timestamp(path)))
        except Exception as exc:
            print(f"ERROR  {path}: cannot read capture time: {exc}", file=sys.stderr)
            missing.append(path)
    stamped.sort(key=lambda pt: pt[1])
    events = strategy.assign([t for _, t in stamped])
    return [(p, e) for (p, _), e in zip(stamped, events)], missing


def link_into(out_dir: Path, event: Event, result: Result) -> Path:
    """Symlink the original into out_dir/<event>/<category>/, avoiding name collisions."""
    target = result.path.resolve()
    folder = out_dir / event.folder / result.category
    folder.mkdir(parents=True, exist_ok=True)
    link = folder / target.name
    if link.is_symlink() and link.resolve() == target:
        return link
    if link.exists() or link.is_symlink():
        tag = hashlib.sha1(str(target).encode()).hexdigest()[:8]
        link = folder / f"{target.stem}-{tag}{target.suffix}"
        if link.is_symlink() and link.resolve() == target:
            return link
    link.symlink_to(target)
    return link


def column(category: Category) -> str:
    """CSV column name = last folder name: moose, not_moose, no_animal, unsure."""
    return category.value.rsplit("/", 1)[-1]


def write_events_csv(out_dir: Path, tallies: dict[Event, dict[str, int]]) -> None:
    columns = [column(c) for c in Category] + ["errors"]
    with open(out_dir / "events.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event", "folder", "start", "end", *columns])
        for event in sorted(tallies, key=lambda e: e.start):
            counts = tallies[event]
            w.writerow([event.index, event.folder, event.start.isoformat(), event.end.isoformat(),
                        *(counts.get(c, 0) for c in columns)])


def estimate_usd(photos: list[tuple[Path, Event]], identify: IdentificationStrategy) -> tuple[int, float]:
    images = len(photos) * identify.images_per_photo
    return images, images * TOKENS_PER_IMAGE * PRICE_PER_TOKEN


async def run(
    photos: list[tuple[Path, Event]], strategy: EventStrategy, identify: IdentificationStrategy,
    out_dir: Path, concurrency: int, dry_run: bool, api_key: str, smoothing: bool = True,
) -> int:
    sem = asyncio.Semaphore(concurrency)
    event_of = dict(photos)
    neighbours = find_neighbours(photos, SMOOTH_NEIGHBOURS)
    results: dict[Path, Result] = {}
    failed: list[Path] = []

    async def one(client: AsyncTypeSafeClient, path: Path) -> None:
        async with sem:
            try:
                results[path] = await identify.identify(client, path)
            except Exception as exc:  # surface failures; never sort them as an answer
                failed.append(path)
                print(f"ERROR  {path}: {type(exc).__name__}: {exc}", file=sys.stderr)

    async with AsyncTypeSafeClient(api_key=api_key, headers={"TypeSafe-Danger-Zone-Images": "true"}) as client:
        await asyncio.gather(*(one(client, p) for p, _ in photos))
    if smoothing:
        results = smooth(results, neighbours)
    results = flag_for_review(results, event_of)

    counts = {c: 0 for c in Category}
    tallies: dict[Event, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for path in failed:
        tallies[event_of[path]]["errors"] += 1
    log = None if dry_run else open(out_dir / "results.jsonl", "a")
    try:
        for path, event in photos:  # time order
            if (result := results.get(path)) is None:
                continue
            counts[result.category] += 1
            tallies[event][column(result.category)] += 1
            note = ("  (smoothed: moose in neighbouring frame)" if result.smoothed else
                    "  (check: 'not a moose' in an event with moose)" if result.category == Category.CHECK_NOT_MOOSE
                    else "")
            print(f"{event.folder}  {result.category:<16} animal={result.animal:.2f} "
                  f"moose={result.moose:.2f}  {path}{note}")
            if log:
                link_into(out_dir, event, result)
                log.write(json.dumps({
                    "path": str(path), "event": event.index, "event_folder": event.folder,
                    "event_start": event.start.isoformat(), "strategy": strategy.name,
                    "identify": identify.name, "category": result.category, "animal": result.animal,
                    "moose": result.moose, "species": result.species, "features": result.features,
                    "smoothed": result.smoothed, "model": result.model, "input_tokens": result.input_tokens,
                }) + "\n")
    finally:
        if log:
            log.close()
    if not dry_run:
        write_events_csv(out_dir, tallies)

    summary = "  ".join(f"{c}={n}" for c, n in counts.items())
    tokens = sum(r.input_tokens or 0 for r in results.values())
    print(f"\n{len(tallies)} event(s)  {summary}  errors={len(failed)}  "
          f"smoothed={sum(r.smoothed for r in results.values())}  "
          f"tokens={tokens:,} (${tokens * PRICE_PER_TOKEN:.2f})", file=sys.stderr)
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="moose-sort",
        description="Group photos into 10-minute events, classify them with Jev, and symlink them "
                    "into <event>/{animal/moose,animal/not_moose,animal/not_moose_check,no_animal,unsure}/ folders.",
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="image files and/or directories")
    parser.add_argument("-o", "--out", type=Path, default=Path("sorted"),
                        help="output directory (default: ./sorted)")
    parser.add_argument("-e", "--events", choices=sorted(STRATEGIES), default="static",
                        help="event windows: 'static' = fixed 10-min grid from the first photo; "
                             "'rolling' = each event starts at the first photo not in the previous "
                             "event (default: static)")
    parser.add_argument("--no-smooth", action="store_true",
                        help="don't promote a photo to moose when the frame next to it in the same event is a moose")
    parser.add_argument("--estimate", action="store_true",
                        help="print the estimated cost and exit without calling the API")
    parser.add_argument("-j", "--concurrency", type=int, default=8,
                        help="parallel requests (default: 8)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="classify and print only; create no links")
    parser.add_argument("--api-key-file", type=Path, default=DEFAULT_KEY_FILE,
                        help=f"file containing the TypeSafe API key (default: {DEFAULT_KEY_FILE}); "
                             "falls back to $TYPESAFE_API_KEY if the file is missing")
    args = parser.parse_args()

    out_dir = args.out.resolve()
    images = find_images(args.inputs, exclude=out_dir)
    if not images:
        parser.error("no .jpg/.jpeg/.png images found")

    strategy = STRATEGIES[args.events]()
    identify = SinglePhoto()
    photos, missing = group_into_events(images, strategy)
    if not photos:
        parser.error("no images with a readable capture time")

    n_events = len({e for _, e in photos})
    n_images, usd = estimate_usd(photos, identify)
    print(f"{len(photos)} photo(s) in {n_events} {strategy.name} event(s): "
          f"{n_images} image(s) to send, estimated ${usd:.2f}", file=sys.stderr)
    if args.estimate:
        return

    api_key = load_api_key(args.api_key_file)
    if not api_key:
        parser.error(f"no API key: put your TypeSafe key in {args.api_key_file} (replacing the placeholder) "
                     "or set TYPESAFE_API_KEY. Create a key at https://console.typesafe.ai/")
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    rc = asyncio.run(run(photos, strategy, identify, out_dir, args.concurrency, args.dry_run, api_key,
                         smoothing=not args.no_smooth))
    sys.exit(1 if missing else rc)

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
from .config import DEFAULT_CONFIG_NAME, ConfigError, find_config, load_config
from .export import is_copy_of, resolve_links, zip_dir
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


def resolve_api_key(api_key: str | None, api_key_file: Path | None) -> str:
    """Pick the key for a CLI run, in this order:

    1. --api-key KEY (or "-" to read one line from stdin, which keeps it out of shell history);
    2. --api-key-file PATH, which must exist and hold a real key;
    3. the default src/api_key file, then $TYPESAFE_API_KEY;
    4. an interactive hidden prompt, if stdin is a terminal.

    Raises ValueError with a user-facing message when no key can be found.
    """
    if api_key is not None:
        key = sys.stdin.readline().strip() if api_key == "-" else api_key.strip()
        if not key:
            raise ValueError("--api-key was given but is empty")
        return key
    if api_key_file is not None:
        if not api_key_file.is_file():
            raise ValueError(f"API key file not found: {api_key_file}")
        key = api_key_file.read_text().strip()
        if not key or key.startswith("<"):
            raise ValueError(f"API key file {api_key_file} is empty or still holds the placeholder")
        return key
    key = load_api_key(DEFAULT_KEY_FILE)
    if key:
        return key
    if sys.stdin.isatty():
        import getpass
        key = getpass.getpass("TypeSafe API key (input hidden): ").strip()
        if key:
            return key
    raise ValueError(f"no API key: pass --api-key or --api-key-file, put your TypeSafe key in "
                     f"{DEFAULT_KEY_FILE} (replacing the placeholder), or set TYPESAFE_API_KEY. "
                     "Create a key at https://console.typesafe.ai/")


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
    """Symlink the original into out_dir/<event>/<category>/, avoiding name collisions.

    A copy left there by --resolve-links counts as already placed, so re-running doesn't duplicate it.
    """
    target = result.path.resolve()
    folder = out_dir / event.folder / result.category
    folder.mkdir(parents=True, exist_ok=True)
    link = folder / target.name
    if (link.is_symlink() and link.resolve() == target) or is_copy_of(link, target):
        return link
    if link.exists() or link.is_symlink():
        tag = hashlib.sha1(str(target).encode()).hexdigest()[:8]
        link = folder / f"{target.stem}-{tag}{target.suffix}"
        if (link.is_symlink() and link.resolve() == target) or is_copy_of(link, target):
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moose-sort",
        description="Group photos into 10-minute events, classify them with Jev, and symlink them "
                    "into <event>/{animal/moose,animal/not_moose,animal/not_moose_check,no_animal,unsure}/ folders.",
        epilog=f"Settings can also come from an HCL file: --config PATH, or ./{DEFAULT_CONFIG_NAME} if it "
               "exists. Command-line options override the file.",
    )
    parser.add_argument("inputs", nargs="*", type=Path,
                        help="image files and/or directories (default: `inputs` from the config file). "
                             "May be left out with --resolve-links or --zip to export an existing output "
                             "directory without calling the API")
    parser.add_argument("-c", "--config", type=Path, metavar="PATH",
                        help=f"HCL config file (default: ./{DEFAULT_CONFIG_NAME} if it exists)")
    parser.add_argument("--no-config", action="store_true",
                        help=f"ignore ./{DEFAULT_CONFIG_NAME} even if it exists")
    parser.add_argument("-o", "--out", type=Path, default=Path("sorted"),
                        help="output directory (default: ./sorted)")
    parser.add_argument("-e", "--events", choices=sorted(STRATEGIES), default="static",
                        help="event windows: 'static' = fixed 10-min grid from the first photo; "
                             "'rolling' = each event starts at the first photo not in the previous "
                             "event (default: static)")
    parser.add_argument("--smooth", action=argparse.BooleanOptionalAction, default=True,
                        help="promote a photo to moose when the frame next to it in the same event is "
                             "a moose (default: on; --no-smooth turns it off)")
    parser.add_argument("--estimate", action="store_true",
                        help="print the estimated cost and exit without calling the API")
    parser.add_argument("-j", "--concurrency", type=int, default=8,
                        help="parallel requests (default: 8)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="classify and print only; create no links")
    key_group = parser.add_mutually_exclusive_group()
    key_group.add_argument("-k", "--api-key", metavar="KEY",
                           help="TypeSafe API key; use '-' to read it from stdin (keeps it out of "
                                "shell history and the process list)")
    key_group.add_argument("--api-key-file", type=Path, metavar="PATH",
                           help=f"file containing the TypeSafe API key. Without either option the key "
                                f"comes from the config file, then {DEFAULT_KEY_FILE}, then "
                                "$TYPESAFE_API_KEY, then a hidden prompt if running in a terminal")
    parser.add_argument("--resolve-links", action=argparse.BooleanOptionalAction, default=False,
                        help="after sorting, replace each symlink in the output with a copy of the "
                             "original photo, so the folder can be shared (originals are untouched)")
    parser.add_argument("--zip", action=argparse.BooleanOptionalAction, default=None,
                        help="after resolving links, zip the output directory (implies --resolve-links)")
    parser.add_argument("--zip-path", type=Path, metavar="PATH",
                        help="where to write the zip (implies --zip; default: <out>.zip next to the "
                             "output directory)")
    parser.add_argument("--export", type=Path, metavar="DIR",
                        help="don't sort: take DIR, the output folder of an earlier run, replace its "
                             "symlinks with copies of the originals, and zip it if --zip is given. "
                             "Never calls the API and needs no key; config `inputs` are ignored")
    return parser


def main() -> None:
    parser = build_parser()

    # Pass 1 finds the config file; its values become defaults, so pass 2's command line overrides them.
    pre, _ = parser.parse_known_args()
    config_path = None if pre.no_config else find_config(pre.config, Path.cwd())
    config: dict = {}
    if config_path is not None:
        try:
            config = load_config(config_path)
        except ConfigError as e:
            parser.error(str(e))
        print(f"config: {config_path}", file=sys.stderr)
    config_key = {k: config.pop(k) for k in ("api_key", "api_key_file") if k in config}
    parser.set_defaults(**config)
    args = parser.parse_args()
    if args.api_key is None and args.api_key_file is None:   # a key on the command line beats the file's
        args.api_key = config_key.get("api_key")
        args.api_key_file = config_key.get("api_key_file")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")

    if args.export is not None:
        if pre.inputs:
            parser.error("--export takes an existing results folder; don't also give photos to sort")
        if args.estimate or args.dry_run:
            parser.error("--export can't be combined with --estimate or --dry-run")
        args.out = args.export
    out_dir = args.out.resolve()
    do_zip = args.zip if args.zip is not None else args.zip_path is not None   # --no-zip beats zip_path
    zip_path = None if not do_zip else (
        args.zip_path.resolve() if args.zip_path else out_dir.with_name(out_dir.name + ".zip"))
    export = args.resolve_links or zip_path is not None
    if export and args.dry_run:
        parser.error("--resolve-links/--zip need an output directory; they can't be used with --dry-run")
    if zip_path is not None and zip_path == out_dir:
        parser.error("--zip path is the output directory itself")
    if args.export is not None:
        args.resolve_links = export = True     # resolving is the point of --export; zipping is opt-in

    if args.export is not None or not args.inputs:
        if not export:
            parser.error("the following arguments are required: inputs (on the command line or as "
                         "`inputs` in a config file)")
        if not out_dir.is_dir():
            parser.error(f"results folder not found: {out_dir}")
        sys.exit(export_output(out_dir, zip_path))

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

    try:
        api_key = resolve_api_key(args.api_key, args.api_key_file)
    except ValueError as e:
        parser.error(str(e))
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    rc = asyncio.run(run(photos, strategy, identify, out_dir, args.concurrency, args.dry_run, api_key,
                         smoothing=args.smooth))
    if export:
        rc = max(rc, export_output(out_dir, zip_path))
    sys.exit(1 if missing else rc)


def export_output(out_dir: Path, zip_path: Path | None) -> int:
    """--resolve-links / --zip: copy originals in place of the symlinks, then optionally zip."""
    resolved, broken = resolve_links(out_dir)
    print(f"resolved {resolved} link(s) into copies of the originals in {out_dir}", file=sys.stderr)
    for link in broken:
        print(f"broken link, original missing: {link}", file=sys.stderr)
    if broken:
        print(f"{len(broken)} broken link(s) left in place" + ("; not zipping" if zip_path else ""),
              file=sys.stderr)
        return 1
    if zip_path is not None:
        n = zip_dir(out_dir, zip_path)
        size_mb = zip_path.stat().st_size / 1e6
        print(f"zipped {n} file(s) into {zip_path} ({size_mb:,.1f} MB)", file=sys.stderr)
    return 0

"""moose-sort: label trail-camera photos with Jev and symlink them into event folders.

The rules, with examples, are in SORTING_RULES.md.
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient

from .config import DEFAULT_CONFIG_NAME, ConfigError, find_config, load_config
from .events import Event, group_events, read_timestamp
from .export import UNSURE_DIR, is_copy_of, resolve_links, zip_dir
from .identify import TOKENS_PER_IMAGE, IdentificationStrategy, SinglePhoto, smooth
from .labels import LABELS_FILE, LabelStore, Labels
from .questions import ANIMALS, PRICE_PER_TOKEN, UNIDENTIFIED

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


def read_timestamps(images: list[Path]) -> tuple[list[tuple[Path, datetime]], list[Path]]:
    """Capture times, in time order. Returns (photos with a time, photos without one)."""
    stamped, missing = [], []
    for path in images:
        try:
            stamped.append((path, read_timestamp(path)))
        except Exception as exc:
            print(f"ERROR  {path}: cannot read capture time: {exc}", file=sys.stderr)
            missing.append(path)
    stamped.sort(key=lambda pt: pt[1])
    return stamped, missing


def link_into(folder: Path, photo: Path) -> Path:
    """Symlink the original into folder (an event folder, or unsure/), avoiding name collisions.

    A copy left there by --resolve-links counts as already placed, so re-running doesn't duplicate it.
    """
    target = photo.resolve()
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


def prune_stale_links(out_dir: Path, keep: set[Path], photos: set[Path]) -> int:
    """Remove symlinks to this run's photos that aren't where this run put them, then empty folders.

    A photo's event folder changes when its labels or its neighbours' labels change (or the folder
    layout does), and the old link would otherwise stay behind. Links to other photos, and copies
    made by --resolve-links, are never touched.
    """
    removed = 0
    for link in sorted(out_dir.rglob("*")):
        if link.is_symlink() and link not in keep and link.resolve() in photos:
            link.unlink()
            removed += 1
    for folder in sorted((d for d in out_dir.rglob("*") if d.is_dir() and not d.is_symlink()),
                         key=lambda d: len(d.parts), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    return removed


def event_type(event: Event) -> str:
    """SORTING_RULES.md's event types; events without a moose are 'other'."""
    return "moose" if event.is_moose else "mixed" if event.is_mixed else "other"


def write_events_csv(out_dir: Path, events: list[Event], labels: dict[Path, Labels]) -> None:
    with open(out_dir / "events.csv", "w", newline="") as f:
        w = csv.writer(f)
        columns = [*ANIMALS, UNIDENTIFIED]
        w.writerow(["folder", "name", "type", "start", "end", "photos", *columns])
        for e in events:
            counts = {a: sum(a in labels[p].keywords for p in e.photos) for a in columns}
            w.writerow([e.folder, e.name, event_type(e), e.start.isoformat(), e.end.isoformat(),
                        len(e.photos), *(counts[a] for a in columns)])


def estimate_usd(n_photos: int, identify: IdentificationStrategy) -> tuple[int, float]:
    images = n_photos * identify.images_per_photo
    return images, images * TOKENS_PER_IMAGE * PRICE_PER_TOKEN


async def label_photos(photos: list[tuple[Path, datetime]], identify: IdentificationStrategy,
                       store: LabelStore, concurrency: int, api_key: str | None,
                       ) -> tuple[dict[Path, Labels], list[Path]]:
    """Labels for every photo: from the store when it has them, else from Jev (and then stored)."""
    labels: dict[Path, Labels] = {}
    to_send = []
    for path, _ in photos:
        if (hit := store.cached(path)) is not None:
            labels[path] = hit
        else:
            to_send.append(path)
    failed: list[Path] = []
    if not to_send:
        return labels, failed
    sem = asyncio.Semaphore(concurrency)

    async def one(client: AsyncTypeSafeClient, path: Path) -> None:
        async with sem:
            try:
                labels[path] = await identify.identify(client, path)
            except Exception as exc:  # surface failures; never treat them as an answer
                failed.append(path)
                print(f"ERROR  {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
                return
        store.record(labels[path])   # right away, so an interrupted run keeps what it paid for

    async with AsyncTypeSafeClient(api_key=api_key, headers={"TypeSafe-Danger-Zone-Images": "true"}) as client:
        await asyncio.gather(*(one(client, p) for p in to_send))
    return labels, failed


async def run(
    photos: list[tuple[Path, datetime]], identify: IdentificationStrategy, out_dir: Path,
    store: LabelStore, concurrency: int, dry_run: bool, api_key: str | None, smoothing: bool = True,
) -> int:
    labels, failed = await label_photos(photos, identify, store, concurrency, api_key)
    if smoothing:
        labels = smooth(labels, photos)
    events = group_events([(p, t, labels[p].keywords if labels[p].has_animal else ())
                           for p, t in photos if p in labels])
    store.save(labels.values())
    event_of = {p: e for e in events for p in e.photos}
    unsure = [p for p, _ in photos if p in labels and labels[p].keywords == (UNIDENTIFIED,)]

    for path, _ in photos:  # time order
        if (r := labels.get(path)) is None:
            continue
        e = event_of.get(path)
        note = "  (smoothed: " + ",".join(r.smoothed) + " in neighbouring frame)" if r.smoothed else ""
        print(f"{e.folder if e else '(no event)':<40} {','.join(r.keywords):<18} "
              f"{'cached' if r.cached else 'jev':<6} {path}{note}")

    if not dry_run:
        placed = {link_into(out_dir / e.folder, p) for e in events for p in e.photos}
        # a record of the photos with an animal Jev couldn't identify: links only, never copied
        placed |= {link_into(out_dir / UNSURE_DIR, p) for p in unsure}
        # only photos labelled in this run: a failed call must not unplace a photo from an earlier run
        if pruned := prune_stale_links(out_dir, placed, {p.resolve() for p in labels}):
            print(f"removed {pruned} link(s) left from an earlier run in folders that no longer apply",
                  file=sys.stderr)
        unsure_set = set(unsure)
        with open(out_dir / "results.jsonl", "w") as log:
            for path, taken in photos:
                if (r := labels.get(path)) is None:
                    continue
                e = event_of.get(path)
                log.write(json.dumps({
                    "path": str(path), "taken": taken.isoformat(),
                    "event_folder": e.folder if e else None, "event": e.name if e else None,
                    "labels": list(r.keywords), "unsure": path in unsure_set, "probabilities": r.probabilities,
                    "smoothed": list(r.smoothed), "cached": r.cached, "identify": identify.name,
                    "model": r.model, "input_tokens": r.input_tokens,
                }) + "\n")
        write_events_csv(out_dir, events, labels)

    sent = [r for r in labels.values() if not r.cached]
    tokens = sum(r.input_tokens or 0 for r in sent)
    by_type = {t: sum(event_type(e) == t for e in events) for t in ("moose", "mixed", "other")}
    dropped = sum(not r.has_animal for r in labels.values())
    print(f"\n{len(events)} event(s) (moose={by_type['moose']} mixed={by_type['mixed']} "
          f"other={by_type['other']})  photos in events={len(event_of)}  unsure={len(unsure)}  "
          f"no animal (dropped)={dropped}  "
          f"smoothed={sum(bool(r.smoothed) for r in labels.values())}  errors={len(failed)}", file=sys.stderr)
    print(f"Jev calls: {len(sent)} (from cache: {len(labels) - len(sent)})  "
          f"tokens={tokens:,} (${tokens * PRICE_PER_TOKEN:.2f})", file=sys.stderr)
    if failed:
        print(f"{len(failed)} photo(s) weren't labelled, so events around them may be split. Run again "
              "to retry them: labelled photos come from the label store and aren't paid for twice.",
              file=sys.stderr)
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moose-sort",
        description="Label each photo with the animals Jev sees in it (human, dog, cougar, deer, elk, "
                    "moose), group photos with animals into events (an event ends after 10 minutes "
                    "without an animal), and symlink them into <start>_<animals>/ folders such as "
                    "2024-10-30_162804_moose-deer/. Photos without an animal are left out.",
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
    parser.add_argument("--labels", type=Path, metavar="PATH",
                        help=f"label store: every photo's labels, and the cache that keeps photos from "
                             f"being sent to Jev twice (default: <out>/{LABELS_FILE}). Share one file "
                             "between output folders to reuse labels across them")
    parser.add_argument("--smooth", action=argparse.BooleanOptionalAction, default=True,
                        help="confirm an animal Jev was unsure about when the frame next to it shows "
                             "it for sure (default: on; --no-smooth turns it off)")
    parser.add_argument("--estimate", action="store_true",
                        help="print the estimated cost of the photos not yet in the label store, and "
                             "exit without calling the API")
    parser.add_argument("-j", "--concurrency", type=int, default=8,
                        help="parallel requests (default: 8)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="label and print only; create no links and write no files (answers "
                             "paid for in a dry run are not stored)")
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
                             "original photo, with its labels written in as XMP keywords, so the folder "
                             "can be shared (originals are untouched)")
    parser.add_argument("--zip", action=argparse.BooleanOptionalAction, default=None,
                        help="after resolving links, zip the output directory (implies --resolve-links)")
    parser.add_argument("--zip-path", type=Path, metavar="PATH",
                        help="where to write the zip (implies --zip; default: <out>.zip next to the "
                             "output directory)")
    parser.add_argument("--export", type=Path, metavar="DIR",
                        help="don't sort: take DIR, the output folder of an earlier run, replace its "
                             "symlinks with keyword-tagged copies of the originals, and zip it if --zip "
                             "is given. Never calls the API and needs no key; config `inputs` are ignored")
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
    labels_path = args.labels.resolve() if args.labels else out_dir / LABELS_FILE
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
        sys.exit(export_output(out_dir, zip_path, labels_path))

    images = find_images(args.inputs, exclude=out_dir)
    if not images:
        parser.error("no .jpg/.jpeg/.png images found")

    identify = SinglePhoto()
    photos, missing = read_timestamps(images)
    if not photos:
        parser.error("no images with a readable capture time")

    store = LabelStore(labels_path, writable=not args.dry_run)
    to_send = sum(store.cached(p) is None for p, _ in photos)
    n_images, usd = estimate_usd(to_send, identify)
    print(f"{len(photos)} photo(s): {len(photos) - to_send} already labelled in {labels_path}, "
          f"{n_images} image(s) to send, estimated ${usd:.2f}", file=sys.stderr)
    if args.estimate:
        return

    api_key = None
    if to_send:
        try:
            api_key = resolve_api_key(args.api_key, args.api_key_file)
        except ValueError as e:
            parser.error(str(e))
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    rc = asyncio.run(run(photos, identify, out_dir, store, args.concurrency, args.dry_run, api_key,
                         smoothing=args.smooth))
    if export:
        rc = max(rc, export_output(out_dir, zip_path, labels_path))
    sys.exit(1 if missing else rc)


def export_output(out_dir: Path, zip_path: Path | None, labels_path: Path) -> int:
    """--resolve-links / --zip: copy originals in place of the symlinks, tag them, then optionally zip."""
    store = LabelStore(labels_path, writable=False)
    resolved, broken = resolve_links(out_dir, store.keywords)
    print(f"resolved {resolved} link(s) into keyword-tagged copies of the originals in {out_dir}",
          file=sys.stderr)
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

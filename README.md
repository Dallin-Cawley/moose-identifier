# moose-sort

Sort trail-camera photos into 10-minute **events** and find the moose, using TypeSafe's
[Jev](https://console.typesafe.ai/) model (`jev-1.14.0`).

Each photo is grouped by its EXIF capture time, Jev is asked whether it shows an animal and whether that
animal is a moose, and the photo is symlinked into `<event>/<category>/`:

| Folder                    | Meaning                                                               |
|---------------------------|-----------------------------------------------------------------------|
| `animal/moose`            | Jev is confident it's a moose (or a neighbouring frame was one)       |
| `animal/not_moose`        | an animal, not a moose, and no moose was in the same event            |
| `animal/not_moose_check`  | "not a moose", but a moose was in the same event; check these by eye  |
| `no_animal`               | people, scenery, empty frames                                         |
| `unsure`                  | Jev's answers were unsettled or contradictory                         |

The full rules, thresholds and the experiments behind them are in [SORTING_RULES.md](SORTING_RULES.md).

## Installation

Requirements: **Python 3.15+** and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://git.championtourney.com/timmy2654/moose-sort.git
cd moose-sort
uv sync                # creates .venv and installs pillow, typesafe-sdk and pytest
```

### API key

Create a key at <https://console.typesafe.ai/>. `moose-sort` looks for it in this order and uses the
first one it finds:

| # | Source                         | Example                                                     |
|---|--------------------------------|-------------------------------------------------------------|
| 1 | `-k/--api-key KEY` on the CLI  | `uv run moose-sort photos --api-key ts_...`                 |
|   | …or `--api-key -` (stdin)      | `pass show typesafe \| uv run moose-sort photos --api-key -` |
| 2 | `--api-key-file PATH`          | `uv run moose-sort photos --api-key-file ~/.config/typesafe/key` |
| 3 | `src/api_key`                  | replace the placeholder in the file with your key           |
| 4 | `TYPESAFE_API_KEY`             | `export TYPESAFE_API_KEY=ts_...`                            |
| 5 | hidden prompt                  | asked interactively if nothing above is set and you're in a terminal |

- `--api-key` and `--api-key-file` can't be used together.
- A file given with `--api-key-file` must exist and hold a real key. It's an error otherwise, with no
  fallback to the environment, so a typo in the path doesn't quietly use a different key.
- `src/api_key` counts as unset when it's missing, empty, or still holds the placeholder (anything
  starting with `<`).
- `--api-key KEY` leaves the key in your shell history and the process list. Prefer `--api-key -`, a
  key file, or the prompt on shared machines.
- `--estimate` never needs a key.

> **Don't commit your real key.** After putting it in `src/api_key`, run
> `git update-index --skip-worktree src/api_key` so git ignores your local change.

## Usage

```sh
# 1. See what it would cost; sends nothing and needs no key
uv run moose-sort path/to/photos --estimate

# 2. Sort (default: static 10-minute grid, event smoothing on)
uv run moose-sort path/to/photos -o sorted

# Rolling windows instead (each event starts at the first photo not in the previous one)
uv run moose-sort path/to/photos -o sorted_rolling --events rolling

# Classify and print only, create no links
uv run moose-sort path/to/photos --dry-run
```

| Option                  | Default         | Description                                                        |
|-------------------------|-----------------|--------------------------------------------------------------------|
| `inputs`                | (required)      | image files and/or directories (`.jpg`, `.jpeg`, `.png`)           |
| `-o, --out DIR`         | `./sorted`      | output directory                                                   |
| `-e, --events`          | `static`        | `static` (fixed grid from the first photo) or `rolling`            |
| `--no-smooth`           | off             | don't promote a photo to moose when its neighbouring frame is one  |
| `--estimate`            | off             | print the estimated cost and exit                                  |
| `-j, --concurrency N`   | `8`             | parallel requests                                                  |
| `-n, --dry-run`         | off             | classify and print only                                            |
| `--resolve-links`       | off             | replace the output's symlinks with copies of the originals         |
| `--zip`                 | off             | resolve links, then zip the output folder (to `<out>.zip`)         |
| `--zip-path PATH`       | `<out>.zip`     | where to write the zip (implies `--zip`)                           |
| `-k, --api-key KEY`     | —               | TypeSafe API key, or `-` to read it from stdin                     |
| `--api-key-file PATH`   | —               | file containing the TypeSafe API key (see [API key](#api-key))     |

Tips:

- **Process a whole card at once.** Static windows are anchored at the earliest photo in the input, and
  smoothing only sees neighbours in the same run.
- **Use a separate output folder per event strategy**; they produce different event folders.
- Photos without an EXIF capture time are reported and left unsorted (exit code 1).

### Output

```
sorted/
├── results.jsonl          # one line per photo: event, category, probabilities, input_tokens, ...
├── events.csv             # one row per event: start, end, and counts per category
├── event_0000_2024-10-08_090313/
│   ├── animal/
│   │   ├── moose/STC_0005.JPG -> /path/to/photos/STC_0005.JPG
│   │   ├── not_moose/
│   │   └── not_moose_check/
│   ├── no_animal/
│   └── unsure/
└── ...
```

By default the photos are never moved or copied, only symlinked. Re-running on the same photos reuses
existing links.

### Sharing the results

Symlinks only work on the machine that made them. To share the sorted folder, turn them into real copies
and optionally zip it:

```sh
# Sort and export in one go
uv run moose-sort path/to/photos -o sorted --zip                 # writes sorted/ with copies + sorted.zip

# Export an existing output folder; no inputs, no API calls, no key needed
uv run moose-sort -o sorted --resolve-links                      # copies only
uv run moose-sort -o sorted --zip-path ~/share/moose-results.zip # copies + zip at a chosen path
```

- **`--resolve-links`** replaces each symlink with a copy of the original photo, at the same path and with
  the same name and timestamps. The originals are never moved or changed. It's safe to run again, and
  re-sorting into a resolved folder doesn't duplicate photos.
- **`--zip`** resolves links first, then writes `<out>.zip` next to the output folder, with the folder as
  its top-level entry. Photos are stored uncompressed (JPEGs don't shrink further, and it's much faster);
  `results.jsonl` and `events.csv` are compressed.
- If an original has gone missing, its link is reported and left in place, no zip is written, and the
  exit code is 1.
- **Disk space:** copies take as much space as the originals, and so does the zip (about 7 GB each for
  a 5,000-photo card).
- **Privacy:** the copies are byte-for-byte originals, so they keep the camera's EXIF data, including GPS
  if the camera records it. `results.jsonl` contains each original's absolute path on your machine.

## Cost estimation

Jev bills **input tokens only**: **$0.042 per million tokens** for `jev-1.14.0` (TypeSafe's listed price
on 2026-11-15; output tokens are free). Each photo is one request of about **4,377 tokens** (measured on
432 photos, banner cropped, 1568×833), so roughly **$0.00018 per photo**.

| Volume                       | Estimated cost |
|------------------------------|----------------|
| 1 photo                      | $0.00018       |
| 1,000 photos                 | $0.18          |
| 5,061 photos (one SD card)   | ~$0.94         |
| 50,000 photos (~10 cards)    | ~$9.20         |
| 100,000 photos               | ~$18.40        |
| 1,000,000 photos             | ~$184          |

**Rule of thumb: about $0.18 per 1,000 photos.** Event smoothing and the check folder are computed
locally and cost nothing. Always run `--estimate` first for your actual photo set.

What changes the estimate:

- **Re-running pays again**: nothing is cached.
- **A different camera/resolution** changes tokens per photo; `results.jsonl` records `input_tokens` for
  every photo so you can check.
- **Price or model changes**: `src/pythonproject/questions.py` pins `jev-1.14.0` and `PRICE_PER_TOKEN`;
  update both together.
- The production CLI has **no spending cap**; `--estimate` is the safeguard. (The experiment scripts are
  capped, see below.)

## Development

```sh
uv run pytest                     # ~100 tests, a few seconds, never calls the API
uv run python tests/replay.py     # accuracy table from recorded Jev answers (free)
```

Every test runs against a fake API (`tests/conftest.py`), so no test can reach TypeSafe or spend money.

Live experiments (`scripts/experiment.py`) go through a spending guard: every request is logged to
`.typesafe_spend.jsonl` and refused once the total would pass `TYPESAFE_BUDGET_USD` (default $2.00).

```sh
uv run python scripts/experiment.py dev B_nobanner --estimate   # cost only
uv run python -m pythonproject.budget --report                  # spend so far
```

Experiments need the labelled photo set in `src/images/`, which is not in the repository (it contains
camera GPS data and people).

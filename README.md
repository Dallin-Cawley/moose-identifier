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

Create a key at <https://console.typesafe.ai/>, then either:

- replace the placeholder in `src/api_key` with your key, **or**
- set an environment variable: `export TYPESAFE_API_KEY=...`

The file wins if it holds a real key; a missing, empty or placeholder file (anything starting with `<`)
falls back to `TYPESAFE_API_KEY`. You can also point at another file with `--api-key-file PATH`.

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
| `--api-key-file PATH`   | `src/api_key`   | file containing the TypeSafe API key                               |

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

The photos themselves are never moved or copied, only symlinked. Re-running on the same photos reuses
existing links.

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

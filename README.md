# moose-sort

Label trail-camera photos with the animals in them and sort them into **events**, using TypeSafe's
[Jev](https://console.typesafe.ai/) model (`jev-1.14.0`). The rules are in [SORTING_RULES.md](SORTING_RULES.md).

1. Jev answers three questions about each photo, in order of priority: **is there an animal?**
   (this decides the events), **is it a moose?**, and **which other animals?** (`human`, `dog`,
   `cougar`, `deer`, `elk`). A photo can have several labels. One with an animal Jev couldn't
   identify is `unidentified`; one with no animal is `no_animal`.
2. Photos with an animal are grouped into events. An event ends after **10 minutes without an
   animal** and has no maximum length. Photos without an animal belong to no event and are left out.
3. Each event's photos are symlinked into a folder named after its start time and animals:

| Event | Contains | Folder |
|---|---|---|
| Moose event | only moose | `2024-10-30_164204_moose` |
| Mixed event | moose and others | `2024-10-30_162804_moose-deer` |
| Other | no moose | `2024-10-30_170804_human-dog` |

On the 200-photo test set, every animal photo was detected, 107 of 112 moose photos were labelled
moose (the other 5 still sit in their moose's event), and no other photo was labelled moose. See
[SORTING_RULES.md](SORTING_RULES.md#6-how-well-it-works).

Labels are stored in `labels.jsonl`, so **no photo is sent to Jev twice**. Exported copies carry
their labels as XMP keywords that photo apps can search. The full rules, with examples, are in
[SORTING_RULES.md](SORTING_RULES.md).

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
| 3 | config file                    | `api_key_file = "..."` (or `api_key`) in [the config file](#configuration-file) |
| 4 | `src/api_key`                  | replace the placeholder in the file with your key           |
| 5 | `TYPESAFE_API_KEY`             | `export TYPESAFE_API_KEY=ts_...`                            |
| 6 | hidden prompt                  | asked interactively if nothing above is set and you're in a terminal |

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

# 2. Sort (smoothing on). Running it again only pays for photos not labelled yet.
uv run moose-sort path/to/photos -o sorted

# Keep one label store per card, shared by every output folder
uv run moose-sort path/to/photos -o sorted --labels path/to/photos/labels.jsonl

# Label and print only, create no links
uv run moose-sort path/to/photos --dry-run

# 3. Zip up an existing result to share it: no re-sorting, no API calls, no key needed
uv run moose-sort --export sorted --zip
```

Already sorted and just want a shareable zip? `--export DIR --zip` takes the results folder of an
earlier run, replaces its symlinks with copies of the original photos (with their labels written in as
keywords), and writes `DIR.zip` next to it.
Leave out `--zip` to only resolve the links. See [Sharing the results](#sharing-the-results).

| Option                  | Default         | Description                                                        |
|-------------------------|-----------------|--------------------------------------------------------------------|
| `inputs`                | (required)      | image files and/or directories (`.jpg`, `.jpeg`, `.png`)           |
| `-c, --config PATH`     | `./moose-sort.hcl` | HCL config file (see [Configuration file](#configuration-file)) |
| `--no-config`           | off             | ignore `./moose-sort.hcl` even if it exists                        |
| `-o, --out DIR`         | `./sorted`      | output directory                                                   |
| `--labels PATH`         | `<out>/labels.jsonl` | label store and cache; share it to reuse labels across folders |
| `--smooth / --no-smooth`| on              | confirm an animal Jev was unsure about when the next frame shows it |
| `--estimate`            | off             | print the cost of photos not yet labelled, and exit                |
| `-j, --concurrency N`   | `8`             | parallel requests                                                  |
| `-n, --dry-run`         | off             | label and print only; writes nothing (answers aren't stored)       |
| `--[no-]resolve-links`  | off             | replace the output's symlinks with keyword-tagged copies           |
| `--[no-]zip`            | off             | resolve links, then zip the output folder (to `<out>.zip`)         |
| `--zip-path PATH`       | `<out>.zip`     | where to write the zip (implies `--zip`)                           |
| `--export DIR`          | —               | don't sort: resolve an earlier run's results folder, zip with `--zip` |
| `-k, --api-key KEY`     | —               | TypeSafe API key, or `-` to read it from stdin                     |
| `--api-key-file PATH`   | —               | file containing the TypeSafe API key (see [API key](#api-key))     |

Tips:

- **Process a whole card at once.** Events and smoothing only see the photos in the same run. Thanks
  to the label store, re-running with more photos only pays for the new ones.
- If some Jev calls fail, the run exits with code 1 and events around those photos may be split.
  Run it again: only the failed photos are sent.
- Photos without an EXIF capture time are reported and left unsorted (exit code 1).

### Configuration file

Every setting you'd otherwise type each time can live in an [HCL](https://github.com/hashicorp/hcl)
file. `moose-sort` reads `--config PATH`, or `./moose-sort.hcl` from the current directory if it exists
(`--no-config` skips it). It prints `config: <path>` when it uses one.

Example `moose-sort.hcl`, with every setting:

```hcl
# Photos to sort: files and/or folders. Used when none are given on the command line.
inputs = [
  "~/trailcam/card-12",
]

# Where the event folders go.
output = "sorted"

# The label store: every photo's labels, and the cache that keeps photos from being sent to
# Jev twice. Default: labels.jsonl inside the output folder.
labels = "~/trailcam/card-12/labels.jsonl"

# Confirm an animal Jev was unsure about when the frame right before or after shows it for sure.
smoothing = true

# Parallel requests to TypeSafe.
concurrency = 8

# Where the TypeSafe key is. Keep the key itself out of this file if you share it; use
# api_key = "..." only for a private config.
api_key_file = "~/.config/typesafe/key"

# Make the output shareable after sorting. zip_path on its own turns zip on.
export {
  resolve_links = true
  zip           = true
  zip_path      = "share/moose-results.zip"
}
```

With that file in place, `uv run moose-sort` sorts the card and writes the zip in one go.

How settings combine:

- **Command line > config file > built-in defaults.** For example, `-o other` overrides `output`, and
  photos named on the command line replace `inputs` (they aren't added to it). Every on/off setting has
  a matching `--no-...` flag, so the config file can be overridden either way (`--no-smooth`, `--smooth`,
  `--no-zip`, `--no-resolve-links`).
- A key given on the command line (`--api-key`, `--api-key-file`) beats the config file's
  `api_key`/`api_key_file`. The file can set one of them, not both.
- **Relative paths are relative to the config file's folder**, not the current directory, and `~` is
  expanded. A config file works the same wherever you run it from.
- Every setting is optional, and a mistake is a usage error naming the file and setting: an unknown
  name, a wrong type, or HCL syntax. `events`, from earlier versions, is reported as removed. Variables and interpolation (`${...}`)
  aren't supported; write values literally.
- `--estimate` and `--dry-run` are command-line only.
- **If the config sets `inputs`, running with no photos sorts them**, which calls the API and costs
  money. To only export an earlier run's results, use `--export DIR`, which ignores `inputs`:
  `uv run moose-sort --export sorted --zip`.
- `moose-sort.hcl` is in `.gitignore`, since it may hold a key or personal paths.

### Output

```
sorted/
├── labels.jsonl           # label store: one line per photo (content hash, probabilities, labels)
├── results.jsonl          # this run, one line per photo: time, event, labels, probabilities, ...
├── events.csv             # one row per event: folder, name, type, start, end, photos, per-animal counts
├── 2024-10-30_162804_moose-deer/
│   ├── STC_3120.JPG -> /path/to/photos/STC_3120.JPG
│   └── STC_3121.JPG -> /path/to/photos/STC_3121.JPG
├── 2024-10-30_164204_moose/
├── 2024-10-30_170804_human-dog/
└── unsure/                # symlinks to photos with an animal Jev couldn't identify
```

`unsure/` lists, as symlinks, every photo that surely shows an animal Jev couldn't identify. Those
photos are also in their event folder; `unsure/` is for checking them by eye. It stays links-only after
export, and the zip stores them as symlinks (no photo is duplicated).

By default the photos are never moved or copied, only symlinked. Re-running on the same photos reuses
existing links. A photo whose event changed, for example after relabelling, is moved and its old
link removed.

### Sharing the results

Symlinks only work on the machine that made them. To share the sorted folder, turn them into real copies
and optionally zip it:

```sh
# Sort and export in one go
uv run moose-sort path/to/photos -o sorted --zip     # writes sorted/ with copies + sorted.zip

# Export the results folder of an earlier run: no sorting, no API calls, no key needed
uv run moose-sort --export sorted                    # resolve the symlinks only
uv run moose-sort --export sorted --zip              # ...and write sorted.zip next to it
uv run moose-sort --export sorted --zip-path ~/share/moose-results.zip
```

- **`--export DIR`** is the way to package a previous result. It takes the results folder itself, so
  there's nothing to re-sort and no photos to name. It always resolves the links; the zip is only
  written with `--zip` or `--zip-path` (or `zip`/`zip_path` in the config file). It ignores `-o` and
  the config file's `inputs`.
- **`--resolve-links`** replaces each symlink in the event folders (not in `unsure/`) with a copy of the original photo, at the same path and with
  the same name and timestamps, and **writes the photo's labels into the copy as XMP keywords**
  (`dc:subject`: e.g. `moose`, `deer`). Lightroom, digiKam, darktable, Windows Explorer ("Tags") and
  macOS can search and filter by them. The image isn't re-encoded: pixels and camera EXIF stay exactly as
  they were. The originals are never moved or changed. It's safe to run again, and re-sorting into a
  resolved folder doesn't duplicate photos.
- **`--zip`** resolves links first, then writes `<out>.zip` next to the output folder, with the folder as
  its top-level entry. Photos are stored uncompressed (JPEGs don't shrink further, and it's much faster);
  `labels.jsonl`, `results.jsonl` and `events.csv` are compressed.
- If an original has gone missing, its link is reported and left in place, no zip is written, and the
  exit code is 1.
- **Disk space:** copies take as much space as the originals, and so does the zip (about 7 GB each for
  a 5,000-photo card).
- **Privacy:** apart from the added keywords, the copies are the originals, so they keep the camera's EXIF
  data, including GPS if the camera records it. `labels.jsonl` and `results.jsonl` contain each
  original's absolute path on your machine.

## Cost estimation

Jev bills **input tokens only**: **$0.042 per million tokens** for `jev-1.14.0` (TypeSafe's listed price
on 2026-11-15; output tokens are free). Each photo is **one request**, with all the questions, of
about **4,400 tokens** (measured), so roughly **$0.00018 per photo**.

| New photos                   | Estimated cost |
|------------------------------|----------------|
| 1,000 photos                 | $0.18          |
| 5,061 photos (one SD card)   | ~$0.94         |
| 50,000 photos (~10 cards)    | ~$9.20         |

**Photos already in the label store cost nothing**: re-runs, re-sorting into another folder, or adding
photos to a card only pay for photos not labelled yet. `--estimate` counts only those. Smoothing is
computed locally and is free. See [SORTING_RULES.md](SORTING_RULES.md#5-keeping-the-number-of-jev-calls-down)
for what keeps the number of calls down.

- **Changing a question's wording relabels everything** on the next run (the store remembers which
  wording made each label); changing a threshold doesn't.
- **A different camera/resolution** changes tokens per photo; `results.jsonl` records `input_tokens` for
  every photo sent.
- **Price or model changes**: `src/pythonproject/questions.py` pins `jev-1.14.0` and `PRICE_PER_TOKEN`;
  update both together.
- The production CLI has **no spending cap**; `--estimate` is the safeguard. (The experiment script is
  capped, see below.)

## Development

```sh
uv run pytest                     # ~100 tests, a few seconds, never calls the API
uv run python tests/replay.py     # accuracy table from recorded Jev answers (free, once recorded)
```

Every test runs against a fake API (`tests/conftest.py`), so no test can reach TypeSafe or spend money.

Live experiments (`scripts/experiment.py`) go through a spending guard: every request is logged to
`.typesafe_spend.jsonl` and refused once the total would pass `TYPESAFE_BUDGET_USD` (default $2.00).

```sh
uv run python scripts/experiment.py dev --estimate   # cost only
uv run python -m pythonproject.budget --report                  # spend so far
```

Experiments need the labelled photo set in `src/images/`, which is not in the repository (it contains
camera GPS data and people).

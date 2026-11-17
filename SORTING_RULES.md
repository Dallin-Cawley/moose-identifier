# Moose-sort: sorting rules

`moose-sort` groups trail-camera photos into **events** (10-minute windows), asks Jev whether each photo
shows an animal and whether that animal is a moose, and symlinks every photo into
`<event>/<category>/`.

```sh
uv run moose-sort src/images --estimate                      # print the cost; sends nothing
uv run moose-sort src/images -o sorted                       # default: static events, single-photo identification
uv run moose-sort src/images -o sorted_rolling --events rolling
```

Use a separate output folder for each event strategy. They produce different event folders, and
mixing them in one output folder gets confusing.

---

## 1. Events

An **event** is a 10-minute window of time. Every photo belongs to exactly one event.

Rules common to both strategies:

- **Timestamp:** the photo's EXIF capture time (`DateTimeOriginal`, else `DateTime`) from the camera clock.
  The filename and file date are never used, because filenames are not in time order.
  A photo without a capture time is reported as an error and left unsorted.
- **Window:** an event covers `[start, start + 10 min)`. The start is included, the end is excluded.
- **Boundary rule:** a photo taken exactly at, or after, the end of a window belongs to the **next** event.
- **Folder name:** `event_<NNNN>_<YYYY-MM-DD>_<HHMMSS>`, where the date and time are the event's start.

The strategy, chosen with `--events`, decides where windows start.

### Static windows (`--events static`, default)

A fixed grid starting at the **earliest photo in the input**, running continuously across days and gaps.

- Event *N* covers `[anchor + N×10 min, anchor + (N+1)×10 min)`.
- `N` is the window number counted from the anchor, so numbers skip wherever no photos were taken.
  Only windows that contain photos get a folder.
- A visit that crosses a grid line is split into two events.

Examples with this photo set (anchor = first photo, `2024-10-08 09:03:13`):

| Photo taken                    | Event | Window                          |
|--------------------------------|-------|---------------------------------|
| 2024-10-08 09:03:13            | 0     | 09:03:13 – 09:13:13             |
| 2024-10-08 09:12:59 (hypothetical) | 0 | 09:03:13 – 09:13:13             |
| 2024-10-08 09:13:13 (hypothetical) | 1 | 09:13:13 – 09:23:13 (boundary → new event) |
| 2024-10-30 16:31:40            | 3212  | 16:23:13 – 16:33:13             |
| 2024-10-30 16:34:02            | 3213  | 16:33:13 – 16:43:13             |

> **Caveat:** the anchor is the earliest photo *in the input*, so running on a different set of photos
> (a subset, or a new card) can shift every window. Process all of a card's photos together.

### Rolling windows (`--events rolling`)

Each event starts at the **first photo that doesn't fit in the previous event's window** and lasts 10 minutes.

- Photos are taken in time order. A photo at or after `current.start + 10 min` starts a new event whose
  start is that photo's timestamp.
- Events are numbered 0, 1, 2, … with no gaps.
- The result does not depend on photos before the current event, so a later card doesn't shift earlier events.
  (The numbering does still depend on the input.)

Example with this photo set, on 2024-10-30: events start at 16:28:04, 16:39:16, 16:49:27, …

### The moose crossing a boundary

A moose is photographed from minute 8 through minute 11 of a window:

| Strategy | Photos at minutes 8–9 | Photos at minutes 10–11 |
|----------|-----------------------|-------------------------|
| static   | event *N*             | event *N+1*: the grid line at minute 10 splits the visit |
| rolling  | event *N*             | event *N+1*, which starts at the minute-10 photo; the following 10 minutes go with it |

With this photo set: static gives **61** events, rolling gives **56**.

---

## 2. Identification (per photo)

### 2.1 The questions

Each request asks Jev these questions about `photo`. All wording is in `src/pythonproject/questions.py`.

| ID | Type | Question | Used for |
|---|---|---|---|
| `animal` | yes/no | Is there a real, living animal in `photo`? (people, drawings, toys and statues don't count) | animal or not |
| `species`, `species_rev` | choice | Which kind of animal is in `photo`? moose / deer / elk / small_animal (calves, antlerless cows and partly hidden moose count as moose) | which species |
| `f_hump` | yes/no | A large, heavy body with a raised hump at the shoulders? | moose anatomy |
| `f_pale_legs` | yes/no | Long legs that look paler than the body? | moose anatomy |
| `f_long_muzzle` | yes/no | A long, broad muzzle that overhangs the mouth? | moose anatomy |

- **The species question is asked twice,** with the options in opposite orders, and the two answers are
  averaged. TypeSafe notes that Jev can lean toward the first option; averaging found 1–2 more moose on the
  tuning set.
- **Moose anatomy** is three separate, observable yes/no questions instead of one vague "does it look like
  a moose?". Moose seen from behind were the main error, and their pale "stockings" and humped back stay
  visible even when the head doesn't.

### 2.2 From answers to a folder

**Reading the answers**

- **animal:** > 0.7 = yes, < 0.3 = no, anything in between is unsettled.
- **species:** use the average of the two option orders. "Moose" if moose is the **top option**; "not
  moose" if the moose probability is **< 0.3**; in between is unsettled.
- **moose anatomy is visible** if **hump > 0.5 and (pale legs > 0.7 or long muzzle > 0.5)**.

| Animal?   | Species                            | Moose anatomy? | Folder              |
|-----------|------------------------------------|----------------|---------------------|
| yes       | moose on top                       | any            | `animal/moose`      |
| yes       | moose < 0.3                        | no             | `animal/not_moose`  |
| yes       | moose < 0.3                        | **yes**        | `unsure`            |
| yes       | moose 0.3+ but not on top          | any            | `unsure`            |
| no        | moose ≤ 0.7                        | any            | `no_animal`         |
| no        | moose > 0.7 (contradiction)        | any            | `unsure`            |
| unsettled | anything                           | any            | `unsure`            |

**Moose anatomy can only move a photo *to* `unsure`, never into `animal/moose`,** so it can't add a deer
to the moose folder.

### 2.3 Event smoothing (default on; `--no-smooth` turns it off)

After every photo is identified, code (not Jev) revises the results. A photo is **promoted to
`animal/moose`** when all of these hold:

1. it landed in `animal/not_moose` or `unsure`, and Jev is sure it shows an animal (animal > 0.7);
2. the frame directly before or after it, **in the same event**, was identified as a moose;
3. Jev did not confidently call it another species: moose ≥ **0.1**, or moose anatomy is visible.
   On the tuning set, 58 of 61 real deer scored below 0.1, and every moose that smoothing rescued scored
   0.20 or more.

Notes:

- Only the neighbours' *original* results count, so promotions never chain along a sequence.
- `results.jsonl` marks promoted photos with `"smoothed": true`, and the terminal prints
  `(smoothed: moose in neighbouring frame)`.
- Smoothing costs nothing: it uses answers the run already has.
- It only sees neighbours that are in the same run, so sort a whole card at once.

### 2.3b The check folder: `animal/not_moose_check/`

After smoothing, every photo still in `animal/not_moose` **whose event also contains a moose photo** is
moved to `animal/not_moose_check/`. That folder is for you to look through by eye: "Jev says not a
moose, but a moose was here at the same time."

- **Why:** missed moose are almost always a moose walking away from the camera. From behind, at night,
  it looks like a deer for several frames in a row, and smoothing can't rescue it because its neighbours
  are rear views too (see "Known weak spot" below). Real deer, raccoons and birds visited in events of
  their own.
- **Measured:**
  - test set (default setup): **5 photos were moved to the check folder, and all 5 are moose.** Those were
    every remaining hard miss, so none are left;
  - tuning set: **0 photos**. None of its 61 deer, raccoon and bird photos are in a moose event.
- `animal/not_moose` now means "not a moose, and no moose was around": it can be trusted without review.
- A real deer that walks through *while* a moose is in the same 10-minute event will also land in the
  check folder. That's the intended trade-off: a few extra photos to glance at, instead of missed moose.
  The replays only see recorded photos, so on a whole card expect somewhat more photos in the folder
  than the tests show.
- It's always on, costs nothing (code only), and the terminal marks these photos with
  `(check: 'not a moose' in an event with moose)`.

### 2.4 One photo per request

Each request sends **one photo** (about 4,400 tokens, ~$0.94 for this card). Sending its neighbouring
frames from the same event as well was tried, and offered for a while as `--identify neighbours`. It was
**removed** because it cost about 5× as much for no clear gain (see 2.6). Event smoothing and the check
folder get the benefit of neighbouring frames in code, for free.

The code keeps the `IdentificationStrategy` base class in `identify.py`, with `single` as its only
strategy, so another approach can be added later.

### 2.5 Image preparation

The API rejects any image wider or taller than 1568 px, and doesn't resize it for you. The trail camera
shoots 2688×1512, so each photo is prepared before upload (`prepare_image` in `classify.py`):

- **rotated upright** from the EXIF orientation;
- **the camera's date/time/temperature banner** (the bottom 84 rows) **is cropped off.** It's camera
  overlay, not scene. Cropping it saved 5% of tokens and cost no accuracy;
- **scaled** to fit 1568 px on the longest side (1568×833 for this camera). Images under 280 px are
  scaled up;
- **re-encoded** as a baseline JPEG with **no metadata**, so the camera's GPS location is not uploaded.

The originals on disk are never modified. If any photo's API call fails, it is **not sorted**: it is
reported as an error and the run exits with status 1. A failure is never treated as "no animal".

### 2.6 How this was tuned, and how well it works

The goal, as agreed: **catch every moose.** The worst outcome is a moose in `animal/not_moose` or
`no_animal` (a "hard miss"). `unsure` and the check folder are acceptable, since both are meant to be
reviewed. `animal/moose` must stay clean.

**Data**, all labelled by eye; files are in `tests/fixtures/`:

- **dev (232 photos), for tuning:**
  - the earlier 124 photos;
  - 108 hard cases picked on purpose: every deer, raccoon and bird event, rear-view and partial moose,
    and empty night frames inside animal events.
- **test (200 photos), for the final check:**
  - drawn at random and **labelled before any Jev call.** The SHA-256 of `test_labels.csv` was recorded
    then (`52ddbbad…`) and still matches;
  - used once, for the final setups;
  - 112 moose, 82 no animal, 2 other animals, 4 ambiguous;
  - it has very few non-moose animals, so the "false moose" checks rely on dev's 61.
- `ambiguous` photos (an animal may be there, but no one could name the species from one frame) are not
  scored.
- Two dev labels were changed after review: STC_1959 and STC_3120 went from `no_animal` to `ambiguous`
  (a faint shape by the tree).

**Experiments on dev** (every variant in an arm shared one request, so variants saw identical input):

| # | Idea | Result | Kept? |
|---|---|---|---|
| E1 | Species options in reverse order, averaged with normal order | Slightly better: +1–2 moose found | ✅ |
| E2 | Separate moose-anatomy questions → "rescue to `unsure`" rule | Hard misses 5–6 → 1–2 (3 independent runs), no false moose | ✅ |
| E2 | White rump patch / head visible questions | Not useful: moose rumps look pale in infrared too | ❌ |
| E3a | Crop the camera banner | Same accuracy, 5% cheaper | ✅ |
| E3b | Boost contrast on infrared photos | No gain | ❌ |
| E4 | Zoom: Box around the animal, then species on a close-up crop | +2 moose found, but hard misses unchanged; ~2× cost | ❌ |
| E5 | Neighbouring frames (±1, ±2) in the request | ±2: 85/87 moose, 0 hard misses; ~4× cost | ❌ shipped as `--identify neighbours`, then removed (see below) |
| E6 | Event smoothing in code | 86/87 moose, 0 hard misses, **no extra cost** | ✅ default |
| E7 | Repeat runs | Results within ±1 photo between runs | — |

**Final check on the 200 unseen test photos** (196 scored, 112 moose):

| Setup | Correct | Moose found | Moose → `unsure` | Moose → check folder | **Hard misses** | False moose | Cost (this card) |
|---|---|---|---|---|---|---|---|
| Previous rules (one species question, no averaging, rescue, smoothing or check folder)¹ | 173 (88%) | 93 | 7 | — | 12 | 0 | ~$0.87 |
| **`single` + smoothing (default)** | **186 (95%)** | **106** | 1 | 5 (of 5 in the folder) | **0** | **0** | **~$0.94** |
| Neighbouring frames + smoothing (removed) | 189 (96%) | 105 | 3 | 4 (of 4 in the folder) | **0** | 0 | ~$4.61 |

Without the check folder, those moose would be hard misses: 5 for `single`, 4 with neighbouring frames.

**Why sending neighbouring frames was removed:**

- It got 3 more photos right and found 1 fewer moose, from a single run. Repeat runs moved results by
  about ±1 photo, so the difference is close to noise.
- It costs about 5× as much.
- With the check folder, both setups leave no hard misses.
- The recordings remain in `tests/fixtures/experiments/` (arm `E_nb2`).

No person, empty frame, deer or other animal reached `animal/moose` in any measured configuration:
- dev: `single` + smoothing, and neighbouring frames without smoothing;
- test: both setups in the table.
Neighbouring frames + smoothing wasn't run on dev.

**Known weak spot: a moose walking away from the camera for many frames in a row.**

- All 5 photos the default setup filed as "not a moose" on the test set are like this. So was a live
  check on STC_1063–1070, the same moose from behind for 8 frames.
- From behind, at night, its pale legs and light rump look like a deer's. Jev says "deer".
- The neighbouring frames are rear views too, so smoothing has no moose frame to borrow from.
- Sending neighbouring frames didn't fix it either: half of that sequence still went to `not_moose`.
- **This is what the check folder (2.3b) is for:** these photos now land in `animal/not_moose_check/`
  instead of `animal/not_moose`.
- An untested idea for next time: an event-level rule (most frames in this event are moose, none
  confidently deer → reconsider its "deer" frames). It needs fresh labelled photos to validate, since the
  test set has now been used.

## 3. Directory structure

Photos are **symlinked**, not copied; the originals stay in `src/images/`. Category folders are created only
when a photo lands in them, so an event with no moose has no `animal/moose/` folder.

Event folder names below are real (static strategy, full photo set). Which photo is in which category is
illustrative only.

```
sorted_static/
├── results.jsonl                         # one line per photo: path, event, event_start, strategy,
│                                         #   identify, category, animal & moose probabilities,
│                                         #   top species, anatomy answers, smoothed, model, input_tokens
├── events.csv                            # one row per event: event, folder, start, end,
│                                         #   moose, not_moose, not_moose_check, no_animal, unsure, errors
├── event_0000_2024-10-08_090313/
│   ├── animal/
│   │   ├── moose/
│   │   │   └── STC_0005.JPG -> …/src/images/12.11.4.2024/STC_0005.JPG
│   │   ├── not_moose/
│   │   │   └── STC_0011.JPG -> …
│   │   └── not_moose_check/            # "not a moose" in an event with moose: check by eye
│   │       └── STC_0012.JPG -> …
│   ├── no_animal/
│   │   └── STC_0001.JPG -> …
│   └── unsure/
│       └── STC_0014.JPG -> …
├── event_0200_2024-10-09_182313/         # numbers skip: windows 1–199 had no photos
│   └── …
├── event_0201_2024-10-09_183313/
│   └── …
├── …
├── event_3212_2024-10-30_162313/
│   ├── animal/
│   │   └── not_moose/
│   └── no_animal/
├── …
└── event_3910_2024-11-04_124313/
    └── …
```

With `--events rolling` the layout is the same, but the folders are numbered sequentially and start at a
photo's time:

```
sorted_rolling/
├── results.jsonl
├── events.csv
├── event_0000_2024-10-08_090313/
├── event_0001_2024-10-09_183312/
├── event_0002_2024-10-09_184312/
├── …
└── event_0055_2024-11-04_124541/
```

If two photos in the same folder share a filename, the second link gets a short hash suffix
(`STC_0001-1a2b3c4d.JPG`). Re-running on the same photos reuses existing links instead of duplicating them.

---

## 4. Cost at scale

Jev bills **input tokens only**: $42 per billion ($0.042 per million) for `jev-1.14.0`. That's the price
listed on TypeSafe's models page on 2026-11-15. Output tokens are free, and images are billed as ordinary
input tokens. `--estimate` prints the projected cost for your photos before anything is sent.

**Measured tokens per photo** with the current questions (banner cropped, 1568×833):

**4,377 tokens per photo**, measured on 432 photos.

| Volume                                   | Estimated cost |
|------------------------------------------|----------------|
| 1 photo                                  | $0.00018       |
| 1,000 photos                             | $0.18          |
| **This card: 5,061 photos**              | **~$0.94**     |
| 50,000 photos (e.g. ~10 cards like this) | ~$9.20         |
| 100,000 photos                           | ~$18.40        |
| 1,000,000 photos                         | ~$184          |

Rule of thumb: **about $0.18 per 1,000 photos.** Event smoothing and the check folder are free.

**Spent on development so far** (all recorded in `.typesafe_spend.jsonl`; run
`python -m pythonproject.budget --report`):

- about $0.05 before the $2 budget;
- **$1.31 of the $2 optimisation budget**: 3,477 requests and 31.2 M tokens, covering the tuning
  experiments, repeat runs, the final test runs and a live CLI check.

What would change the estimate:

- **Re-running costs again.** Nothing is cached, so classifying the same card twice pays twice.
- **A different camera or resolution** changes tokens per photo. `results.jsonl` records
  `input_tokens` for every photo.
- **More questions** add a little. With the uncropped image, 2 questions used about 4,100 tokens and
  the 9 questions of the experiments about 4,600. The banner crop takes about 225 tokens back off.
- **Price changes or a new model version.** `questions.py` pins `jev-1.14.0`, so a new release won't
  change prices or answers silently.
- **Failed requests:** whether rejected requests are billed isn't documented. This estimate assumes they
  aren't.
- **Time** isn't estimated. Throughput depends on your organisation's rate limits (shown in the TypeSafe
  console). The SDK retries rate-limited requests automatically, and `-j` sets how many run in parallel.

---

## 5. Testing without spending money

```sh
uv run pytest              # 100 tests, ~5 s, never calls the API
uv run python tests/replay.py   # accuracy table for every recorded setup
```

- Every test runs against a **fake API**. `tests/conftest.py` swaps the SDK's network layer for every
  client, so no test can reach TypeSafe or use your key.
- **Offline coverage:**
  - the decision table, anatomy rescue and threshold edges;
  - static and rolling events;
  - neighbour selection (never across events);
  - exactly what is sent (one image, the questions in both option orders, banner crop, no GPS);
  - every smoothing guard, and the check folder;
  - API errors that are never sorted;
  - the cost estimate, which sends nothing;
  - the folder tree, symlinks, `events.csv` and `results.jsonl`;
  - the spending guard.
- **Replay** (`tests/test_recorded.py` with `tests/replay.py`):
  - Jev's recorded answers for all 432 labelled photos, plus their unlabelled neighbouring frames, are
    run through the **production** code (`result_from_answers`, `smooth`);
  - it checks the accuracy baselines above, **0 hard misses**, **no false moose**, no real deer in the
    check folder on dev, and that no person reaches an animal folder;
  - it also checks that the recordings asked exactly the questions production asks today (against
    `tests/fixtures/experiments/questions_used.json`);
  - so changing a threshold, `decide()` or `smooth()` is measured for free, while changing a question's
    wording needs new recordings.
- **New experiments** use `scripts/experiment.py` (live, budget-guarded) and `scripts/score.py`
  (free). Tune on `dev` only. The `test` set has been used once; label a fresh random sample before
  making further claims.
  ```sh
  uv run python scripts/experiment.py dev B_nobanner --estimate   # cost only
  uv run python scripts/experiment.py dev B_nobanner --run 3      # a new live run
  uv run python scripts/score.py dev B_nobanner --misses
  ```
- **Spending guard** (`pythonproject.budget`):
  - every experiment request is logged to `.typesafe_spend.jsonl`;
  - requests are refused before they're sent once the ledger plus in-flight requests would pass the cap
    (`TYPESAFE_BUDGET_USD`, default $2.00);
  - the production CLI is not capped; use `--estimate` instead.

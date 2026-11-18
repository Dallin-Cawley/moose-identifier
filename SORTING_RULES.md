# Moose-sort: sorting rules

This file is the source of truth for how photos are labelled and sorted: the rules, examples, and
how the code applies them.

**The animals** are, in this order: `human`, `dog`, `cougar`, `deer`, `elk`, `moose`. Any other
creature (a raccoon, a bird) isn't one of them; Jev still notices it as "an animal", but can't name
it (`unidentified`, see 1.2).

`moose-sort` works in three steps:

1. **Label** every photo with the animals Jev sees in it, or `no_animal` if there are none.
2. **Group** the photos that have an animal into **events**. Photos without an animal are left out.
3. **Sort** each event's photos into a folder named after its animals, such as `moose` or
   `moose-deer`.

```sh
uv run moose-sort src/images --estimate     # cost of the photos not labelled yet; sends nothing
uv run moose-sort src/images -o sorted      # label, group and link
uv run moose-sort --export sorted --zip     # share: real copies with labels as keywords, zipped
```

---

## 1. Labelling each photo

### 1.1 Three questions, in order of priority

Every photo is one request to Jev; the photo is sent once and all questions ride along. They answer,
in order of importance:

| Priority | Question | Asked as | Decides |
|---|---|---|---|
| **1** | **Is there an animal?** | `animal`: Is there a real, living animal in `photo`? (drawings, toys and statues don't count; people are left out here) and `human`: Is there a person in `photo`? | which photos form **events** |
| **2** | **Is it a moose?** | `species` / `species_rev`: Which kind of animal is in `photo`? moose / deer / elk / cougar / dog / small_animal, asked in both option orders and averaged | the `moose` label |
| **3** | **Which other animals?** | one yes/no question each: Is there a person / dog / cougar / deer / elk in `photo`? | the other labels |

- **Tier 1 asks about any animal, not just the six.** That makes it the most reliable: on the
  labelled photos it never missed an animal (section 6). People get their own question, because
  the rules count them and the `animal` question doesn't.
- **Tier 2 is a single choice between species, not a yes/no "is there a moose?".** A first version
  asked yes/no per animal; it found only 70 of 87 moose, because Jev's yes/no about moose stayed
  around 0.5 for dark or rear-view photos. Picking the most likely species is a much clearer
  judgment. Asking in both option orders and averaging corrects Jev's slight lean to the first
  option.
- **Tier 3 can label several animals at once** (a person and a dog), so each has its own yes/no.
  Each question's "no" case names the animal it's most easily mistaken for (coyote is not a dog,
  bobcat is not a cougar, elk and moose are not deer).
- The exact wording is in `src/pythonproject/questions.py`.

### 1.2 From answers to labels

Thresholds: a yes/no above **0.7** is a yes, below **0.3** a no, in between **unsettled**.

| Tier | Rule |
|---|---|
| 1 | The photo **has an animal** if `animal` > 0.7, or any animal below is confirmed (this is how people count). |
| 2 | **`moose`** if `animal` > 0.7 **and** moose is the top species option (both orders averaged), even under 0.5. |
| 3 | **`human`, `dog`, `cougar`, `deer`, `elk`**: their own question > 0.7. |

The photo's keywords are its animals in the order above (human, dog, cougar, deer, elk, moose). If it has an animal but none of the six
was identified, it's **`unidentified`**: a raccoon, a bird, or a moose too dark to call. If it has
no animal, it's **`no_animal`**.

| Jev's answers (anything not listed is low) | Labels | In an event? |
|---|---|---|
| animal 0.95, species top = moose | `moose` | yes |
| animal 0.95, species top = moose (P 0.45) | `moose` | yes |
| animal 0.96, species top = moose, deer 0.81 | `deer`, `moose` | yes |
| animal 0.90, human 0.90, dog 0.85 | `human`, `dog` | yes |
| animal 0.05, human 0.90 | `human` | yes |
| animal 0.95, species top = deer, deer 0.93 | `deer` | yes |
| animal 0.95, species top = deer (P(moose) 0.3), deer 0.5 | `unidentified` (moose if smoothing confirms it) | yes |
| animal 0.95, species top = small_animal | `unidentified` | yes |
| animal 0.60, species top = moose | `no_animal`: Jev isn't sure there's an animal | no |
| everything low | `no_animal` | no |

### 1.3 Smoothing (default on; `--no-smooth` turns it off)

A moose walking away from the camera is often a sure animal whose species Jev calls "deer", while the
frames just before or after it, which show its head, are a clear moose. After every photo is
labeled, code (not Jev) **confirms an animal the photo was unsettled about when the photo directly
before or after it has that animal confirmed**, less than 10 minutes away.

"Unsettled" means:

- **moose:** an animal is sure (> 0.7), moose isn't the top species, but Jev didn't rule it out:
  P(moose) ≥ **0.1**. On the old tuning set, 58 of 61 real deer scored below 0.1, and every moose
  smoothing rescued scored 0.20 or more.
- **the others:** their yes/no is between 0.3 and 0.7.

Example: three frames one minute apart, all a sure animal.

| Frame | Species | Before smoothing | After smoothing |
|---|---|---|---|
| a | moose on top | `moose` | `moose` |
| b | deer on top, P(moose) 0.3 | `unidentified` | **`moose`** (a, right before it, is a moose) |
| c | deer on top, P(moose) 0.3 | `unidentified` | `unidentified`: b was itself only unsettled, and additions never chain |

All three stay in the event (tier 1). Smoothing only decides what they're called.

- Smoothing uses answers the run already has, so it costs nothing.
- An answer below 0.3 (or a moose below 0.1) is never overruled.
- `results.jsonl` lists what smoothing added (`"smoothed": ["moose"]`), and the terminal prints
  `(smoothed: moose in neighbouring frame)`.

---

## 2. Events

### 2.1 Rules

The rules:

- **Only photos with an animal take part (tier 1).** A `no_animal` photo belongs to no event and
  isn't linked anywhere. The original stays where it is. An `unidentified` photo does take part:
  it's animal activity, so it keeps the event going and is linked into it.
- **An event ends when 10 minutes pass with no animal detected.** A photo with an animal taken 10
  minutes or more after the previous animal photo starts a new event. Exactly 10:00 counts as a gap.
- **There is no maximum length.** A moose that stays in view for three hours, with no gap of 10
  minutes, is one event.
- **Empty frames don't keep an event open.** The camera firing on wind or a branch doesn't count
  as an animal.
- **Time** is the photo's EXIF capture time (`DateTimeOriginal`, else `DateTime`). Filenames and
  file dates are never used. A photo without a capture time is reported as an error and left out.

### 2.2 Event types and names

| Type | Contains | Name | Example folder |
|---|---|---|---|
| **Moose event** | only moose | `moose` | `2024-10-30_162804_moose` |
| **Mixed event** | moose and other animals | `moose-<others>` | `2024-10-30_162804_moose-deer` |
| Other event | no moose | `<animals>` | `2024-10-30_170804_human-dog` |
| Other event | nothing identified | `unidentified` | `2024-10-30_031522_unidentified` |

- The name lists every animal identified **anywhere in the event**, joined by `-`.
  `unidentified` photos don't add to the name. An event made only of them, such as a raccoon, is
  named `unidentified`.
- Moose always comes first. The others follow the animal order: human, dog, cougar, deer, elk.
  So it's `moose-human-dog-elk`, never `elk-moose-dog-human`.
- **The folder is the event's start time, then its name:** `<YYYY-MM-DD>_<HHMMSS>_<name>`. The time
  is the first animal photo of the event. It keeps repeated names like `moose` apart and sorts the
  folders in time order.
- `events.csv` records each event's type as `moose`, `mixed` or `other`.

### 2.3 Worked example

A night on the trail camera. Labels are after smoothing.

| Time | Photo | Labels | Event |
|---|---|---|---|
| 16:28:04 | moose1 | `moose` | **A** starts |
| 16:31:04 | deer | `deer` | A |
| 16:37:04 | empty | `no_animal` | none: dropped, and it doesn't extend A |
| 16:42:04 | moose2 | `moose` | **B** starts: 11 min after the last animal (16:31:04) |
| 16:44:04 | blurry | `no_animal` (animal 0.5: Jev isn't sure anything is there) | none: dropped |
| 17:08:04 | walker | `human`, `dog` | **C** starts: 26 min after the last animal |

Result:

| Event | Folder | Type | Photos |
|---|---|---|---|
| A | `2024-10-30_162804_moose-deer` | mixed | moose1, deer |
| B | `2024-10-30_164204_moose` | moose | moose2 |
| C | `2024-10-30_170804_human-dog` | other | walker |

More edge cases:

| Animal photos at (minutes) | Events |
|---|---|
| 0, 9:59, 19:58 | one event: no gap reaches 10 minutes, even though it lasts almost 20 |
| 0, 10:00 | two events: exactly 10 minutes is a gap |
| 0, then empty frames at 5 and 12, then an animal at 14 | two events: the empty frames don't count |
| every 5 minutes for 3 hours | one event, 2 h 55 min long |

---

## 3. Directory structure

Photos are **symlinked**, not copied; the originals stay where they are. Each event is one folder,
and its photos sit directly in it. One more top-level folder, **`unsure/`**, records every photo
that surely shows an animal Jev couldn't identify (`unidentified`).

```
sorted/
├── labels.jsonl                         # the label store (section 4): one line per photo, keyed by content hash
├── results.jsonl                        # this run, one line per photo: path, capture time, event,
│                                        #   labels, probabilities, smoothed, cached, model, input_tokens
├── events.csv                           # one row per event: folder, name, type, start, end, photos,
│                                        #   and how many of its photos show each animal (and unidentified)
├── 2024-10-30_162804_moose-deer/
│   ├── STC_3120.JPG -> …/src/images/12.11.4.2024/STC_3120.JPG
│   └── STC_3121.JPG -> …
├── 2024-10-30_164204_moose/
│   ├── STC_3125.JPG -> …
│   └── STC_3126.JPG -> …                # unidentified: in its event like any photo…
├── 2024-10-30_170804_human-dog/
│   └── STC_3130.JPG -> …
└── unsure/                              # …and recorded here; only ever symlinks to the originals
    └── STC_3126.JPG -> …/src/images/12.11.4.2024/STC_3126.JPG
```

- Photos labelled `no_animal` appear only in `results.jsonl` and `labels.jsonl`, with
  `"event_folder": null`.
- **`unsure/`** holds a link for each `unidentified` photo: Jev is sure there's an animal (tier 1)
  but none of the six animals was identified (tiers 2 and 3), e.g. a moose too dark or too far from
  behind, a raccoon, a bird. The photo **also stays in its event folder**, so the event is complete;
  `unsure/` is the list to look through by eye. It is created only when there is such a photo,
  and `results.jsonl` marks these photos with `"unsure": true`.
- **`unsure/` only ever holds symlinks**, also after `--resolve-links`/`--export`: the photo's
  copy lives in its event folder, so it isn't duplicated. In a zip they're stored as symlink entries
  (no photo bytes): unzipping on Linux or macOS recreates the links, which work wherever the
  originals are at the same path; Windows shows them as small text files.
- When a photo is identified on a later run (relabelled, or confirmed by smoothing), its `unsure/`
  link is removed.
- If two photos in one event share a filename, the second link gets a short hash suffix
  (`STC_0001-1a2b3c4d.JPG`).
- **Re-running** reuses existing links. When labels change, for example after rewording a
  question, a photo's event can change. The old link is then removed, along with any folder left
  empty. Only links to photos labelled in the current run are touched.
- An output folder from the old moose/not-moose layout (`event_0000_…/animal/moose/…`) is cleaned
  up the same way on the first run of the new version.

---

## 4. The label store and image keywords

### 4.1 `labels.jsonl`: the source of truth, and the cache

Every photo's answers are stored in `labels.jsonl`, by default in the output folder. Use `--labels
PATH`, or `labels = "..."` in the config file, to keep one store per card and share it between
output folders.

```json
{"sha256": "9f2c…", "path": "/…/STC_3120.JPG", "size": 1412113, "mtime_ns": 1730301484000000000,
 "model": "jev-1.14.0", "questions": "a1b2c3d4e5f6",
 "probabilities": {"animal": 0.97, "species": {"moose": 0.86, "deer": 0.11, "elk": 0.02, ...},
                   "human": 0.02, "dog": 0.01, "cougar": 0.01, "deer": 0.81, "elk": 0.04},
 "labels": ["deer", "moose"], "time": "2026-11-18T14:02:11"}
```

- **Keyed by the SHA-256 of the photo's bytes.** A renamed or moved photo is still recognised, and
  a different photo with the same name is not mistaken for it. Path, size and modification time are
  kept so the hash isn't recomputed for unchanged files.
- **A photo is sent to Jev only if the store has no entry for it with the current model and
  question wording** (`questions`, a fingerprint of `questions.py`). Re-running a card, adding a
  few new photos, or re-sorting into another folder pays only for photos that are new.
- **Probabilities are stored, not just labels.** Changing a threshold (0.7/0.3) or the smoothing
  rule needs no new calls. Rewording a question changes the fingerprint, so every photo is asked
  again on the next run.
- Answers are written as soon as they arrive, so an interrupted or partly failed run keeps what it
  paid for. Running it again finishes the rest. At the end of a run the file is rewritten with one
  line per photo, including its final (smoothed) labels.
- `--dry-run` reads the store but writes nothing, so answers paid for in a dry run are not kept.

### 4.2 Keywords in exported copies

`--resolve-links`, `--zip` and `--export DIR` replace the symlinks in the event folders with
**copies** of the originals (the links in `unsure/` stay links, see section 3), and write each
photo's labels into its copy as **XMP keywords** (`dc:subject`):

```
STC_3120.JPG   Keywords: deer, moose
STC_3130.JPG   Keywords: human, dog
STC_3126.JPG   Keywords: unidentified
```

- Lightroom, digiKam, darktable, Bridge, Windows Explorer (as "Tags"), macOS Finder/Spotlight and
  most photo apps read these keywords, so whoever receives the zip can search and filter by animal.
- **The image isn't re-encoded.** The keywords are inserted as their own metadata block (about 500
  bytes); the pixels and the camera's EXIF stay exactly as they were.
- **The originals are never written to.** Only the copies in the output folder get keywords.
- A copy that already has XMP from another program is left as it was, with a warning, rather than
  having that metadata overwritten. If a photo has no entry in the label store, it's copied without
  keywords, also with a warning.
- JPEG and PNG are supported.

---

## 5. Keeping the number of Jev calls down

Jev bills **input tokens**, and a photo is about 4,400 of them whatever is asked about it (the eight
questions add only a little). So the cost is, in effect, **the number of photos sent**. What the
code does about it:

| Measure | Saving |
|---|---|
| **One request per photo, all three tiers in it.** The tier-2 and tier-3 questions ride along with tier 1 even when no animal turns out to be there, because a second request would send the photo again. | ~67% versus one request per tier |
| **The label store (section 4.1).** A photo is never paid for twice with the same questions: re-runs, new photos added to a card, other output folders, retries after errors. | Re-running a sorted card: 100% |
| **Probabilities are stored**, so thresholds and smoothing can be tuned without calls. | All tuning of the code side is free |
| **Smoothing in code** instead of sending neighbouring frames with each photo (tested earlier: about 5× the cost for no clear gain). | ~80% versus neighbouring frames |
| **The camera banner is cropped off** before upload. | ~5% per photo |

Ideas not yet done, because each needs live measurements first:

- **Smaller images.** Tokens grow with pixel count. Halving each side would cut cost to about a
  quarter, but small or distant animals at night may be missed. It needs a recorded comparison on
  the dev set.
- **Skipping near-identical frames.** A trail camera fires bursts of almost the same picture, and
  one of them could stand in for the others. This risks missing an animal that enters
  mid-burst.

`--estimate` prints the cost of only the photos the store doesn't have yet.

**Cost** at **4,401 tokens per photo** (measured on 1,338 photos with the current questions) and
$0.042 per million tokens (`jev-1.14.0`, 2026-11-15): **about $0.18 per 1,000 new photos**, ~$0.94 for
this 5,061-photo card. Photos already in the store cost nothing. `results.jsonl` records the real
`input_tokens` of every photo sent.

---

## 6. How well it works

Measured on 2026-11-18 (`jev-1.14.0`), replaying recorded answers through the production code.

**Data:** photos from this card, labelled by eye (`tests/fixtures/`):

- **dev (232 photos)**, for tuning;
- **test (200 photos)**, drawn at random and labelled before Jev saw them, for the final check.

Their labels predate the six-animal rules: moose, other animal, no animal (people are filed as no
animal) or ambiguous (not scored). So they measure tiers 1 and 2, not dog, cougar or elk.

| | dev | **test** |
|---|---|---|
| **Tier 1: animals detected** (moose, deer, raccoon, bird photos) | 148 / 148 | **114 / 114** |
| Empty frames given an animal | 0 | **0** |
| **Tier 2: moose labelled `moose`**, with smoothing | 85 / 87 | **107 / 112** |
| …without smoothing | 78 / 87 | 97 / 112 |
| Moose photos left out of every event | 0 | **0** |
| Non-moose photos labelled `moose` | 0 | **0** |
| "No animal" photos labelled `human` (the labels don't record people, so not scored) | 31 of 77 | — |
| "Other animal" photos labelled `deer` (the other 15 `unidentified`, presumably the raccoon and bird photos) | 46 of 61 | — |
| Tokens per photo | 4,401 | 4,401 |

For comparison, the previous moose/not-moose version put 106 of the test set's 112 moose in its
moose folder and 5 in a review folder. A first try at this version, with a yes/no moose question,
found only 70 of dev's 87 (section 1.1).

**The 5 test moose not labelled moose:**

- one is `unidentified`;
- four are labelled `deer`. All four are a moose walking away from the camera at night, the known
  weak spot: from behind, its pale legs and light rump look like a deer's, and Jev says "deer"
  with P(moose) ≤ 0.14, too low for smoothing.
- **All five stay in an event with the moose.** No moose activity is lost; tier 1 keeps every
  frame.
- **The cost is in the name:** the event becomes `moose-deer` instead of `moose`. On the recorded
  test photos, 2 of the 4 events with a moose were named `moose-deer` for this reason. A whole card
  will show more, since the replay only sees recorded photos.

**Not measured:** dog, cougar and elk (this card has none labelled), and `human` against people
labels (only spot-checked through the old "no animal" photos). Photos labelled for all six animals
are needed for that.

**Possible next step for the `moose-deer` names:** an event-level rule, e.g. "a deer frame inside an
event that is otherwise all moose, with no frame where deer is the clear top species…". That
needs fresh labelled photos to validate, since tuning it on these would just fit these few
sequences.

To re-measure after changing questions (live, budget-guarded):

```sh
uv run python scripts/experiment.py dev --estimate
uv run python scripts/experiment.py dev            # and devnb (neighbouring frames, for smoothing)
uv run python tests/replay.py                      # free
```

`tests/test_recorded.py` holds these results as baselines. Tune on `dev`; the `test` set has now been
used once for this question set.

---

## 7. Testing without spending money

```sh
uv run pytest                       # never calls the API
uv run python tests/replay.py       # scores recorded answers, once there are recordings
```

- Every test runs against a **fake API**. `tests/conftest.py` swaps the SDK's network layer for
  every client, so no test can reach TypeSafe or use your key.
- **Covered offline:**
  - the three-tier truth table, including the 0.3/0.7 edges;
  - event grouping: the 10-minute gap, no maximum length, empty frames, naming and order;
  - smoothing guards;
  - the label store: content keys, invalidation when the questions change, no second call;
  - exactly what is sent (one image, the three tiers of questions, banner crop, no GPS);
  - API errors, which are never sorted;
  - the folder tree, `unsure/` (links only, kept through export, symlink entries in the zip),
    stale-link clean-up, `events.csv` and `results.jsonl`;
  - XMP keywords (pixels and EXIF unchanged, foreign XMP left alone);
  - export and zip;
  - the config file.
- **Replay** (`tests/test_recorded.py` with `tests/replay.py`): Jev's recorded answers for all 432
  labelled photos, plus their unlabelled neighbouring frames, run through the production code. It
  checks every animal is detected, the moose baselines above, and no false moose. Changing a
  threshold or `smooth()` is measured for free; rewording a question makes the recordings stale
  and the tests skip until it's re-recorded.

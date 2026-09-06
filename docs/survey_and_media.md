# The survey, the map and the photos

What `feat/survey-from-node-map` changed, how it works, and what still needs a
person. Written for whoever picks this up next.

---

## In one paragraph

The survey used to be typed by hand and covered 16 places on two floors of the
Hive. It is now **read out of `Hive node map.pdf`** and covers **40 places and
55 links across three buildings**. Every place on a Hive floor has a position,
so **the map draws for the first time**. **125 photographs** and **3 calibrated
floorplans** are uploaded. Bedrock works and Claude has been called for real.

---

## 1. Reading the survey out of the node map

### Why this is possible

The node map is not a picture. It is a vector drawing with a text layer, so
each part of the survey is separately addressable:

| In the drawing | What it means |
|---|---|
| a small filled square | a place |
| the `Hive-B5-G` word beside it | its name |
| a straight line between two squares | a link |
| the `21s` word sitting on that line | its walking time |
| the large image behind everything | the floorplan |

### How it works

`scripts/nodemap.py` reads the drawing. `scripts/import_survey.py` turns it
into `data/campus_graph.json`.

```bash
.venv/bin/python scripts/import_survey.py           # say what would change
.venv/bin/python scripts/import_survey.py --write   # change it
```

Two details that matter:

- **Square size is measured, not assumed.** It is 21.9 pt on one page and
  10.8 pt on another, so the size is taken as the most common near-square
  shape on each page. Hardcoding finds one page and misses the next.
- **A time belongs to exactly one link.** Where two links meet, a `3s` written
  in the corner sits near both. The nearer link takes it; otherwise two good
  rows become two arguments.

### Why you can trust it

Pointed at the same PDF, it **rediscovers the two floors somebody typed up by
hand** — every link, every second, bar one. `tests/test_import_survey.py` holds
it to that, so if a future edit to the drawing breaks the reading, the tests
say so rather than the map quietly going wrong.

The one exception is a real disagreement, not a bug: the drawing joins **Pick
Lockers to the Lift Lobby**, the committed survey says **Main Staircase**. Both
are 12s and the two places are 3s apart. The survey wins, the difference is
reported, and a test fails if a *second* one ever appears.

### What it refuses to do

Guess. Cross-floor connectors are drawn running off the edge of the slide
toward a label rather than ending on a square, and a few times are written
`?s`. Those go to **`data/survey_review.md`** and no edge is written for them.
That is why B3 arrived unreachable, and why the review file says so under its
own heading instead of leaving somebody to discover a 404.

---

## 2. The map

Places now carry `x`/`y`. A position is read as a **fraction of the
floorplan**, so it survives the same plan being rescanned at a different size.
Metres-per-pixel is fitted across every link on the floor rather than taken
from one. Fitted independently, B5 came out **0.8% from the scale somebody had
calibrated by hand** months earlier.

**One bug this exposed.** Coordinates switched on A*'s straight-line estimate
for the first time, and it made **76 of 1560 routes worse** — silently: no
error, just a route a few seconds longer than the one that exists. Floors are
traced onto separate plans with separate origins, so a distance measured
between two of them is the gap between two corners of two pictures. A map
spanning more than one plan now gets no estimate at all.
`test_giving_places_coordinates_never_changes_a_single_route` walks all 1560
trips and is what caught it.

---

## 3. The photos — how they were labelled and uploaded

### The trap

The photo walk produced one folder per place, named on the walk. **The survey
was re-lettered afterwards** (commit `3d3e3b4`). So:

```
Images/B5/Hive-B5-A   is "Staircase 1"   which the survey now calls  Hive_B5_C
Images/B5/Hive-B5-H   is "Lift Lobby"    which the survey now calls  Hive_B5_A
```

Reading the folder name as an id therefore files every B5/B4 photo against the
**wrong place** — silently, because both ids exist and nothing downstream can
tell. This is exactly what happened on the first attempt.

### How it is resolved

**Match by name, not by id.** The folder's id is read against the survey *as it
stood when the walk happened* (`git show 94d8b4a^`), turned into the name of
the place it meant, and matched to whatever carries that name today.

Thirteen folders move:

| Folder | Is really | Now filed as |
|---|---|---|
| `Hive-B5-A` | Staircase 1 | `Hive_B5_C` |
| `Hive-B5-C` | Staircase 3 | `Hive_B5_E` |
| `Hive-B5-D` | Main Entrance | `Hive_B5_I` |
| `Hive-B5-E` | Staircase 2 | `Hive_B5_D` |
| `Hive-B5-H` | Lift Lobby | `Hive_B5_A` |
| `Hive-B5-I` | Main Staircase | `Hive_B5_B` |
| `Hive-B4-A` | Staircase 1 | `Hive_B4_C` |
| `Hive-B4-C` | Staircase 3 | `Hive_B4_E` |
| `Hive-B4-G` | Main Staircase | `Hive_B4_B` |
| `Hive-B4-H` | Lift Lobby | `Hive_B4_A` |
| `Hive-B4-B` | Lift | `Hive_B4_A` |
| `Hive-B4-E` | Side Entrance | `Hive_B4_G` |
| `Hive-B4-F` | Back Entrance | `Hive_B4_F` |

**How this was verified.** A teammate had already filed 43 photos by hand.
Matching those to the local files byte for byte: **40 of 43 agree** with the
name rule. The 3 that do not are places whose *names* changed as well as their
ids — `Side Entrance` split into 1 and 2, `Lift` folded into `Lift Lobby` —
and those three are listed explicitly in `RENAMED` in `scripts/upload_photos.py`
**taken from where the teammate actually filed them**, not from reading the map
and deciding what looked likely.

The B3, walkway, SS and S3 folders were named against the current lettering, so
they map straight through.

### What was recorded for each photo

| Field | Value | How |
|---|---|---|
| `target_id` | the place | folder name → the mapping above |
| `building`, `floor`, `location` | from the survey | looked up from `target_id` |
| `facing` | **empty** | the filenames say nothing about direction |
| `caption` | empty | — |

**`facing` is deliberately blank.** `photo_1_2026-09-03_13-41-36.jpg` carries a
number and a timestamp and nothing about where the camera pointed. A guess
could not be corrected later because nothing downstream would know it was a
guess. An empty one can. `PhotoStore.best_of` prefers a photo facing the way
you are walking and falls back to an undirected one rather than showing you the
way you came — so **every step still gets a picture**, just a less precise one.
The teammate's 43 directional photos keep winning wherever they apply.

### Running it

```bash
.venv/bin/python scripts/upload_photos.py ~/Downloads/Images           # dry run
.venv/bin/python scripts/upload_photos.py ~/Downloads/Images --write
```

Safe to re-run. A file is matched to what is already stored **by exact byte
size**, not by counting photos per place — somebody uploading by hand picks the
best few of a folder and labels them, and counting would either duplicate their
whole folder or skip the ones they left out.

### Where things go

`SHORTCUT_S3_BUCKET` decides, and nothing else does. Set it and everything goes
to the shared bucket; leave it blank and everything stays in `data/photos` and
`data/floorplans` on this machine.

> **For a demo, leave it blank.** Temporary AWS credentials expire after about
> an hour, and when they do the map and every photo disappear mid-recording.
> Local disk cannot expire, and serves the floorplan in 0.003s instead of 3s.

---

## 4. The floorplans

```bash
.venv/bin/python scripts/upload_floorplans.py --write --plans-dir ~/Downloads/Maps/Original
```

Uploads and calibrates in one call, from the same fit that positioned the
places — those two numbers are only meaningful together, and there is no screen
anywhere for calibrating a plan that arrives without one. Plans come out of the
node map by default so the script needs nothing but the repository;
`--plans-dir` uses the team's higher-resolution scans instead, which works
because a position is stored as a fraction rather than a pixel.

A floor that already has a plan is left alone unless you pass `--replace`.

---

## 5. Bedrock

The account's organisation denies `bedrock:InvokeModel` by service control
policy in every region tested **except `us-west-2`**. The photo bucket is in
`us-east-1`. So `BEDROCK_REGION` was added, falling back to `AWS_REGION`:

```
AWS_REGION=us-east-1
BEDROCK_REGION=us-west-2
MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
```

`scripts/check_bedrock.py` passes all four stages, and `BedrockLlm` has now run
against a real model for the first time — about a second per call, ~1570 input
and ~70 output tokens. Asked for "staircase 1" it correctly replied *"Did you
mean Staircase 1 (Hive · B4) or Staircase 1 (Hive · B5)?"* rather than picking
one.

The test suite still passes with `MOCK_MODE=false`: `conftest.py` points tests
at a `.env` that does not exist, so no test can reach the network.

---

## What still needs a person

### 1. Twenty-four places have stand-in names — **do this first**

The map only ever says `Hive-B3-C`, so the import writes `Hive B3 C` and the
app shows *"Walk to Hive B3 C"*. It is also why the live model answered *"I
don't know anywhere called 'the canteen'"* — it cannot match a name nobody has
given.

**How to fix them.** Edit the `name` column of **`data/survey_places.csv`**,
then re-run the importer:

```bash
# 1. edit data/survey_places.csv - change only the `name` column
#      Hive_B3_C,Hive B3 C,Hive,B3,junction
#      Hive_B3_C,Study Pods,Hive,B3,junction
# 2. apply it
.venv/bin/python scripts/import_survey.py --write
# 3. read the diff, then commit
git diff data/campus_graph.json
```

Every place in the drawing is listed in that file, seeded from what the survey
currently calls it — so the 16 places somebody surveyed by hand already show
their real names and leaving those rows alone does nothing. The `type` column
is worth setting too (`junction`, `stairs`, `lift`, `entrance`, `room`,
`bus_stop`), since it is what the map pin and the directions wording use.

The ones needing names: `Hive_A`–`Hive_D`, `Hive_B3_A`–`Hive_B3_G`,
`Hive_SS_Walkway_A`–`D`, `S3_B3_A`, `S3_B3_B`, `SS_B3_A`, `SS_B3_B`, `SS_B3_D`,
`SS_B3_E`, `SS_B3_F`, `SS_Canteen_A`, `SS_Canteen_B`.

### 2. Seven links the drawing does not settle

Listed in **`data/survey_review.md`**. To add one, put a row in
**`data/survey_links.csv`** and re-run the importer:

```csv
from,to,walk_seconds,covered,stairs,lift,note
SS_B3_A,SS_Canteen_A,5,true,false,false,"why you know this"
```

Still open:

- **Four connectors off the B5 plan** — two marked `stairs` from the Main
  Entrance, two marked `Wheelchair staircase` from Pick Lockers. The survey
  already has five B5↔B4 links; somebody who walked B5 needs to say whether
  these are the same ones or extra.
- **`? -- S3-B3-B`, `?s`** — a stub with only one end drawn. Cannot be added
  without knowing what it joins.
- **`SS-B3-E -- SS-B3-D`** was `?s` and is in the survey with an **estimated 7s**,
  scaled off the drawing. Its own note says so. Replace it with a measured
  figure when somebody walks it.

### 3. Two things the data says that are probably wrong on the ground

- **Hive B3 is reachable only by going outside** through the SS walkway. The
  Hive's own staircases presumably continue down from B4 to B3, but the drawing
  does not show them. Worth checking, because right now a route from B5 to B3
  sends somebody out into the rain and back in.
- **Four photographs of `Hive-B4-H`** exist and the newer page of the node map
  has no such place. They are currently filed as the B4 Lift Lobby on the
  strength of the teammate's own uploads. Worth confirming.

### 4. Not started

- **The plain-language box and the clarification screen.** `POST /ai/parse`
  works against the live model and returns a question plus the two places it
  could have meant; **nothing in `web/` calls it**. This is the visible half of
  the project's agentic claim.
- **The Ranking Agent.** Design is in the plan file; `MAX_REPLANS` and the
  LangGraph dependency are already there and unused.
- **Faces.** The photographs show identifiable students and are not blurred.

---

## The commits

| | |
|---|---|
| `fix(map)` | size map pins for the screen, not for a 1570px plan |
| `feat(survey)` | read the node map instead of retyping it |
| `feat(survey)` | join B3 to the rest of the map, and record why |
| `fix(routing)` | stop the new coordinates returning longer routes |
| `feat(media)` | upload the photos and the floorplans in bulk |
| `fix(photos)` | file bulk-uploaded photos against the place they were taken |
| `feat(ai)` | let Bedrock be called in a different region |
| `perf(media)` | stop re-downloading a floorplan for every route |
| `fix(map)` | say when the floorplan store is unreachable |
| `feat(survey)` | let the names file rename a place, not just create one |

417 tests, all offline.

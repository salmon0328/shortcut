# How the map gets made

For somebody new to the project. What the app needs, how it used to be
collected by hand, what is now done by script, and — the part that matters —
which of it a person still has to check.

If you want the mechanics rather than the orientation, read
[survey_and_media.md](survey_and_media.md) instead.

---

## 1. What the app needs, and where each piece lives

Three separate things, kept in three different places. Confusing them is the
root of most of what has gone wrong so far.

| Thing | What it is | Where it lives | In git? |
|---|---|---|---|
| **The survey** | places, and the links between them | `data/campus_graph.json` | **Yes** |
| **Photos** | a picture of each place | S3 bucket, or `data/photos/` | No |
| **Floorplans** | the building's own drawings | S3 bucket, or `data/floorplans/` | No |

A **place** — called a *node* in the code — is somewhere you can stand: a
staircase, a lift lobby, an entrance. A **link** — an *edge* — is a walk
between two places, and carries how many seconds it takes, whether it is
covered, whether it is stairs.

The survey is in git deliberately. Every change to the map is a commit
somebody can read and argue with, which is not true of a database.

---

## 2. How it was done by hand

### Adding a place

Somebody ticks "Admin" in the browser and fills a form: **id**, **name**,
**building**, **floor**, **type**. So a person decided that `Hive_B5_G` is
called "Courtyard".

It does not go straight into the survey. It lands in
`data/graph_overrides.json` — a scratch file on that one laptop, not in git.
Then `scripts/graduate_overrides.py` moves it into `campus_graph.json`, and
somebody commits it. Two steps on purpose: there is a moment to review before
anything becomes official.

> **The gap that mattered.** That form has fields for id, name, building, floor
> and type — and **none for position**. There was never a way to say *where on
> the floorplan* a place is. That is the whole reason the map had never drawn.
> Not a bug: a missing input.

### Adding a link

The same form, a different tab. Pick two places, type the seconds, tick
covered / stairs / lift.

### Photos

One at a time. Pick a place, choose a file, and — this is the valuable part —
choose from a dropdown **which direction the camera was pointing**. That is the
`facing` field, and it is what lets a route step show a picture looking *down
the corridor you are about to walk* rather than back the way you came.

43 photos were uploaded this way. That is 43 separate uploads, each with a
direction chosen by a person who was there.

### Floorplans

**No interface at all.** The endpoints exist; nothing in the browser calls
them. The only way was `curl`, which is why the shared bucket had no
floorplans in it.

### Why this does not scale

Adding B3, the walkway, SS and S3 by hand means 24 place forms, 26 link forms
with times read off a PDF, and 125 photo uploads. Every one of those is a
chance to mistype a number that nobody can check afterwards.

---

## 3. What is now done by script

### The thing that makes it possible

`Hive node map.pdf` **is not a picture**. It is a vector drawing with a text
layer, so a program can see the individual shapes and words rather than pixels:

| In the drawing | What it is |
|---|---|
| a small filled square | a place |
| the `Hive-B5-G` word beside it | its name |
| a straight line between two squares | a link |
| the `21s` word sitting on that line | its walking time |
| the large image behind everything | the floorplan |

So the survey can be **read** instead of retyped. Three scripts do it:
`import_survey.py` for the map, `upload_photos.py`, `upload_floorplans.py`.

### The choices made along the way

These are judgement calls, not facts. Any of them can be wrong, and anyone who
knows the building better should overrule them.

1. **Only the newer pages of the PDF are read.** It draws the Hive twice: pages
   1–5, and pages 7–11 which add SS and S3. The two disagree about lettering,
   and mixing them would be silent chaos, so only the newer ones are used.
2. **Photos are matched by name, not by id** — see below.
3. **`facing` is left empty** on the 85 bulk-uploaded photos. The filenames say
   nothing about direction. A wrong guess could never be corrected afterwards,
   because nothing downstream would know it was a guess. An empty one can.
4. **The scale comes from the straightest corridor**, not the average one.
   Corridors bend, so a walk is always longer than the straight line on the
   plan. Using the average made the router return worse routes.
5. **`distance_m = seconds × 1.4`** — the same walking speed the earlier survey
   used, so new links are priced exactly like the existing ones.
6. **A committed link is never overwritten.** Where the drawing disagrees with
   the survey, the survey wins and the difference is reported.
7. **The Hive–SS walkway is marked uncovered.** *This was assumed, not
   verified.*
8. **Hive B2 and B1 are left out.** They are drawn but have **no times on any
   link**. Inventing times would break the one rule the survey keeps — that it
   records what somebody actually walked.

### How the photos were matched

The photo folders were named **during the walk**. The survey was **re-lettered
afterwards**, in commit `3d3e3b4`. So:

```
Images/B5/Hive-B5-A  is "Staircase 1"  ->  the survey now calls that  Hive_B5_C
```

Reading the folder name as an id therefore files every photo against the
**wrong place** — silently, because both ids exist and nothing downstream can
tell. This is not hypothetical: the first attempt did exactly that.

The fix is to look up the folder's id in the survey *as it stood at the walk*
(`git show 94d8b4a^`), take the **name** it meant, and find whatever carries
that name today. Thirteen folders move.

**How that was checked.** 43 photos had already been filed by hand. Matching
those to the local files byte for byte and asking whether the rule agreed:
**40 of 43 did.** The three that did not are places whose *names* changed as
well as their ids, and those three are written down explicitly, taken from
where they were actually filed rather than from guessing.

---

## 4. What might not be right

Roughly in order of how likely it is and how much it costs.

| # | What | Why it might be wrong |
|---|---|---|
| 1 | **24 stand-in names** | The drawing only ever says `Hive-B3-C`. "Hive B3 C" is not a name, it is a placeholder generated by a script. |
| 2 | **`Hive_A` – `Hive_D`** | Four real places on the B4 page, with no photos and no meaningful name. Nobody has said what they are. |
| 3 | **The walkway is marked uncovered** | Assumed from "it runs between two buildings". Never checked on site. |
| 4 | **`SS_B3_E–SS_B3_D` is 7 seconds** | An **estimate**, scaled off the drawing because the surveyor left it as `?s`. The two references used disagree by 36%, so the truth is somewhere around 6–9s. Its own note in the survey says so. |
| 5 | **One link disagrees with the drawing** | The drawing joins Pick Lockers to the Lift Lobby; the survey says Main Staircase. Both 12s, and the two places are 3s apart. The survey wins until somebody walks it. |
| 6 | **B3 is reachable only by going outside** | Almost certainly wrong in reality: the Hive's own staircases surely continue down from B4. The drawing does not show them, so the map does not have them. |
| 7 | **Four `Hive-B4-H` photos** | Filed as the B4 Lift Lobby on the strength of somebody else's uploads, not on anything visible in the drawing. |
| 8 | **Some floors and buildings were inferred** | The canteen's floor came from which page it was drawn on. The walkway was given an invented building name, `Hive-SS`, because it is in neither. |
| 9 | **85 photos have no direction** | So a step may show the right place photographed the wrong way. Harmless, but less useful than the 43 that were labelled by hand. |

---

## 5. What a person should check

About fifteen minutes, most valuable first.

**1. Look at the map.** Open the app, plan a route, look at where the pins sit.
Are they in roughly the right rooms? They were placed by machine from a traced
drawing. You know the building; the machine does not.

**2. Name the 24 places.** Fixes items 1 and 2 above, and it is the thing most
visible to anybody using the app — it currently says "Walk to Hive B3 C". Each
folder under `~/Downloads/Images/` holds photos of that exact place, so opening
one usually settles it.

```bash
# edit the `name` column of data/survey_places.csv, then:
.venv/bin/python scripts/import_survey.py --write
git diff data/campus_graph.json
```

**3. Spot-check five photos.** Pick five places in the app, look at the
picture, confirm it is that place. If the name-matching is wrong anywhere, five
samples spread across B5 and B4 will show it.

**4. Answer three questions from memory.** Fixes items 3, 6 and 8:

- Is the Hive→SS walkway actually out in the open?
- Do the Hive staircases continue down from B4 to B3?
- Is the SS canteen on the same level as the walkway?

**5. Time one route with a stopwatch.** Walk Main Entrance → SS Canteen and
compare against what the app says. A single walk validates the whole chain: the
times, the fitted scale, the links between them.

**6. Read `data/survey_review.md`.** Seven links the importer refused to guess
at, with what it saw for each one.

---

## In one sentence

The scripts are reliable about **geometry and arithmetic** — the importer
rediscovered two hand-typed floors edge for edge, which is the reason to trust
it on the floors nobody typed — and unreliable about **meaning**: what a place
is called, whether a corridor is rained on, whether a staircase you can see
with your own eyes exists in a drawing that omits it. Everything in section 4
is a question of meaning, and none of it can be settled from a desk.

# Shortcut

Campus routing that follows how NTU is actually walked.

A student types where they are and where they need to be, in their own words,
and gets a route that knows what the campus map does not: which corridors are
covered, which links only exist indoors, which lift is worth waiting for, and
what other students reported blocked or packed in the last hour.

Built for the IGNITE Agentic AI Hackathon 2026. Surveyed on foot across
three basement levels of four connected buildings — the Hive, the South
Spine, S3 and the walkway between them: **40 places, 60 links**.

## Run it

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # the routing service
.venv/bin/pip install -r requirements-ai.txt   # the plain-language layer
.venv/bin/python -m pytest                     # 549 tests, all offline

.venv/bin/uvicorn --app-dir src shortcut.api:app --reload
```

Then <http://127.0.0.1:8000/docs> for the API, or:

```bash
cd web && npm install && npm run dev           # http://localhost:5173
```

**No AWS account is needed to run any of this.** The plain-language box falls
back to an offline reader (`src/shortcut/ai/offline.py`) that uses ordinary
pattern matching instead of a model, so a fresh clone works immediately. To
use Claude on Bedrock instead, see *Using a real model* below.

## How it works

Everything reads from one file, `data/campus_graph.json`: the **survey**, a
list of places and the links between them, written down by someone who walked
the building. Around it:

```
a sentence  ->  parser        turns words into a route request   (Claude, or offline rules)
                places        turns "the lift" into a node id    (ordinary code)
                cost model    prices each link by preference     (ordinary code)
                A*            finds the cheapest way             (ordinary code)
                crowding      makes busy links cost more         (ordinary code, from reports)
             -> a route, its trade-offs, and why it was chosen
```

Two things change the map, and they are kept apart on purpose:

- **The survey** changes when someone walks the building again. It is a
  reviewable git diff, and nothing writes to it while the server runs.
- **What is true today** — a hoarded corridor, a broken lift — lands in
  `data/graph_overrides.json`, which is machine-local and layered on top at
  load time. Delete it and the map is exactly what was surveyed.

Crowding is a third case and is never stored at all: it is worked out per
request from recent reports and expires by itself. See
`src/shortcut/crowding.py`.

## What is where

| Path | What it holds |
|---|---|
| `data/campus_graph.json` | the survey: 40 places, 60 links. The source of truth |
| `src/shortcut/api.py` | every HTTP endpoint (29 of them) |
| `src/shortcut/tools/astar.py` | route search and the cost functions. No AI |
| `src/shortcut/graph_store.py` | loading the survey; `Node` and `Edge` |
| `src/shortcut/overrides.py` | live changes layered over the survey |
| `src/shortcut/crowding.py` | how busy somewhere is, priced at routing time |
| `src/shortcut/report_store.py` | problems students have reported |
| `src/shortcut/photo_store.py` | photos of places, and of reported problems |
| `src/shortcut/floorplan_store.py` | the plan image each floor is drawn on |
| `src/shortcut/directions.py` | the words on each step of a route |
| `src/shortcut/nodemap.py` | reading places and links out of a drawn PDF |
| `src/shortcut/survey_import.py` | turning that reading into reviewable changes |
| `src/shortcut/floorplan_build.py` | composing and scaling a floor's plan image |
| `src/shortcut/candidate_store.py` | the review queue an import waits in |
| `src/shortcut/blob_store.py` | local disk, or S3 when a bucket is named |
| `src/shortcut/ai/` | the agentic layer — see `src/shortcut/ai/README.md` |
| `web/` | the browser front end (vanilla JS + Vite, no framework) |
| `scripts/` | command-line tools — see [scripts/README.md](scripts/README.md) |
| `tests/` | 549 tests, none of which touch the network |

## Where the AI is, and where it deliberately is not

Two components use a model. Everything else — the search, the costs, the
thresholds, the map — is ordinary code, and that division is the claim this
project makes rather than an implementation detail.

| Component | Uses a model? | Plans, acts, adapts? | What it is |
|---|---|---|---|
| `ai/parser.py` | yes | **no** — one call, no loop, no decisions | an extraction step |
| `ai/verifier.py` | yes | **yes** — weighs, prices, moves its own bar, writes | an **agent** |
| `ai/impact.py` | no | it is a tool | arithmetic on the graph |
| `tools/astar.py` | no | it is a tool | the route search |

**The parser** turns "courtyard to the canteen, it's raining, I can't do
stairs" into a route request. It never sees or returns a node id — it returns
the *phrases* a student used, and `ai/places.py` decides what they refer to,
because a model that emits ids will eventually emit one that does not exist.

**The Verifier** reads the problems students report, rates each account, asks
`ai/impact.py` what closing that place would actually cost, raises its own
threshold when the answer is bad, and then approves, rejects, or hands the
decision to a person. Three rules hold it together:

- **The model rates; the code decides.** Weights come back from the model, but
  the comparison against `AUTO_MERGE_THRESHOLD_BLOCKING` happens in Python,
  against a number the model never sees. Report notes are text typed by
  strangers.
- **Closing something costs more than warning about it.** A wrongly shut
  corridor sends somebody the long way round in the rain; a wrongly flagged
  busy lobby costs nothing.
- **Escalating is a real outcome.** Anything that would leave a place with no
  way in goes to a person however many people reported it. That is the one
  threshold the agent cannot move, and it is what makes the rest safe.

See `src/shortcut/ai/README.md` for the whole picture.

## Preferences

Every route is scored by one preference, and they are genuinely different
answers rather than labels on the same route:

| Preference | Means |
|---|---|
| `fastest` | least time, including any wait |
| `sheltered` | prefers covered links, and says what the detour costs |
| `prefer_lift` | avoids stairs unless there is no other way at all |
| `least_walking` | charges for every walked metre, so a ride wins |

`/route/options` returns the best match plus alternatives — including ones
that lose on every number but differ in kind, such as the step-free way when
you asked for the quickest.

## Using a real model

The plain-language layer runs offline by default. To use Claude on Bedrock:

1. AWS Console → Bedrock → **Model access** → enable the Anthropic models.
   This is separate from having an AWS account.
2. `aws configure sso` (or `aws configure`).
3. Copy `.env.example` to `.env` and set `MOCK_MODE=false`.
4. Check it: `.venv/bin/python scripts/check_bedrock.py`

`GET /ai/health` reports which mode is live and why, without spending tokens.

## Sharing photos and floorplans

Uploads stay on the machine that made them, in `data/photos` and
`data/floorplans`, unless a bucket is named. To share them with the team, set
`SHORTCUT_S3_BUCKET` in `.env` (see `.env.example`) and have AWS credentials
that can read and write it. The server reads `.env` once at startup, so
restart it after changing the file. A setting in your shell always wins over
the file, and the tests never touch a bucket whatever either says.

## Testing

```bash
.venv/bin/python -m pytest          # everything
.venv/bin/python -m pytest -k ai    # just the agentic layer
```

Every test is offline and deterministic. Nothing calls AWS, and nothing
depends on the network being up.

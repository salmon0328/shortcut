# data/

## `campus_graph.json` — the survey

The map, as walked. Someone stood at every junction, paced the distance to the
next one and wrote down what they saw. Everything else in this project reads
from this file, and nothing writes to it while the server is running.

**40 nodes, 60 edges.** Three basement levels — B5 (9 places), B4 (17) and
B3 (14) — across four connected structures: the Hive (27 places), the South
Spine (7), S3 (2) and the walkway joining them (4). They are held together by
9 staircase links and 2 lifts.

A node is a spot you can stand: a junction, a doorway, a lift lobby, the foot
of a staircase. An edge is a way between two of them, carrying how long it
takes, how far it is, and what it is like — covered, stairs, lift, one-way.

### Changing it

Two different things change the map, and they go to different places.

**Surveyed truth** — a corridor was re-measured, a floor was walked, a name
was wrong. Edit this file, commit the diff, and say in the commit message who
walked it and when. A pull request against this file should be reviewable by
someone who knows the building.

**What is true today** — a corridor is hoarded off, a lift is out, a lobby is
packed. That never touches this file. Approved reports and admin edits land in
`graph_overrides.json`, which is machine-local and gitignored, and are layered
on top at load time. Delete that file and the map returns to exactly what was
surveyed.

Runtime changes that turn out to be permanent are promoted deliberately:

```bash
python scripts/graduate_overrides.py          # dry run: what would move
python scripts/graduate_overrides.py --write  # fold them into the survey
git diff data/campus_graph.json               # review before committing
```

That script refuses to write unless the live map is identical before and
after, so graduating can correct the record but never silently change a route.

## Not yet verified on site

- **`Hive_Lift_A`, the lift between B5 and B4.** `walk_seconds: 15` and
  `wait_seconds: 20` are the off-peak figures. The wait at peak reaches about
  three minutes; that variance is modelled by crowd reports at routing time
  (see `src/shortcut/crowding.py`) rather than baked into the survey, because
  it is a property of the hour, not of the building.

Everything else here was measured on the walk.

## `campus_geo.json` and `campus_buildings.geojson` — the campus on the globe

The survey's `x`/`y` are metres on each floor's own drawing; nothing in it
says where on Earth the Hive is. These two files do, for the 3D map only —
routing never reads them.

- **`campus_geo.json`** (hand-edited). Per building: its OpenStreetMap way, a
  height, and per floor an `elevation_m` and two transforms — `transform`
  takes a place's `x`/`y` to latitude/longitude, `plan.transform` takes that
  floor's floorplan *pixels* there. `places` can pin a single place by
  latitude/longitude when its drawn position is missing or wrong.
- **`campus_buildings.geojson`** (generated). Building outlines from
  OpenStreetMap, © OpenStreetMap contributors (ODbL): the buildings named
  above, plus the rest of campus as scenery. Re-create it with
  `scripts/fetch_osm_buildings.py --write`.

How the Hive's three floors were placed, so the numbers can be checked:

1. Each floorplan image was matched to the Hive's OpenStreetMap outline by
   shape (its lobes line up, and on B4 so does S3's outline).
2. **B4's places sit on their plan**, so B4's `transform` is the image fit.
3. **B5's and B3's places do not** — their stored calibration puts them
   several metres off their own plans (B3's lift lobby lands mid-atrium). So
   their `transform` was fitted instead through the stairwells and lift that
   stand directly over B4's (`georef_floor.py Hive B3 --stack-over B4`):
   within 0.8 m on B5, 1.7–3.1 m on B3. `tests/test_geo.py` holds them to
   5 m.

**Estimates, not measurements:** every `height_m` and `elevation_m`, and the
order of the Hive's floors (B5 lowest, B3 highest). S3 and the South Spine
have outlines and heights but no placed floors yet, so a route's stretch
through them shows as "not on the 3D map yet".

## Everything else in this folder

| Path | What it is | In git? |
|---|---|---|
| `campus_graph.json` | the survey, above | yes |
| `campus_geo.json`, `campus_buildings.geojson` | the campus on the globe, above | yes |
| `survey_places.csv`, `survey_links.csv` | the walk, as it was written down | yes |
| `survey_review.md` | what the last import changed, and what it refused to | yes |
| `survey_sources/` | the drawn node map the survey was read from | yes |
| `graph_overrides.json` | this machine's live changes | no |
| `reports.json` | problems students have reported here | no |
| `import_candidates.json` | readings from a PDF, waiting for review | no |
| `import_sources/` | the uploaded PDFs those readings came from | no |
| `photos/` | photos of places and of reported problems | no |
| `floorplans/` | floor plan images and their calibration | no |

The gitignored six are runtime state: a fresh clone starts with an empty
report queue, an empty review queue and the surveyed map, which is the correct
place to start from.

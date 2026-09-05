# data/

## `campus_graph.json` — the survey

The map, as walked. Someone stood at every junction, paced the distance to the
next one and wrote down what they saw. Everything else in this project reads
from this file, and nothing writes to it while the server is running.

**17 nodes, 27 edges.** The Hive, floors B5 (9 places) and B4 (8 places),
joined by four staircases and one lift.

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

- **`Hive_B5_015`, the lift between B5 and B4.** `walk_seconds: 15` and
  `wait_seconds: 20` are the off-peak figures. The wait at peak reaches about
  three minutes; that variance is modelled by crowd reports at routing time
  (see `src/shortcut/crowding.py`) rather than baked into the survey, because
  it is a property of the hour, not of the building.

Everything else here was measured on the walk.

## Everything else in this folder

| Path | What it is | In git? |
|---|---|---|
| `campus_graph.json` | the survey, above | yes |
| `graph_overrides.json` | this machine's live changes | no |
| `reports.json` | problems students have reported here | no |
| `photos/` | junction photos and their index | no |
| `floorplans/` | floor plan images and their calibration | no |

The gitignored four are runtime state: a fresh clone starts with an empty
report queue and the surveyed map, which is the correct place to start from.

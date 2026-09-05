# Floorplan sources

The two plans Shortcut draws routes on, and the numbers that say where the
map sits on them. Committed, because they are survey output.

`data/floorplans/` — the store the app actually reads — is committed too for
now, so a fresh clone draws the map with no setup. That makes this folder the
*source* and that one the *built copy*. Rebuild it after changing anything
here:

```bash
.venv/bin/python scripts/install_floorplans.py --replace
```

## Where these came from

Both images were lifted out of the team's `Hive node map.pdf` (page 1 = B5,
page 2 = B4). Each page embeds one clean PNG of the NTU indoor map, which is
what you see here — the hand-drawn nodes and edges are separate objects on
top and are *not* part of the image.

The node positions in `campus_graph.json` were traced from the same pages.
The black dots are 92×92 Form XObjects, and their placement matrices give the
position of every node. The **text labels are not usable for this**: they sit
off to one side of their dot, which put "Lift" and "Lift Lobby" 14 m apart
when they are really 5 m.

`metres_per_pixel` is a least-squares fit of straight-line pixel distance
against the surveyed `distance_m` over all 22 same-floor edges. Both floors
share one value: cross-correlating the two images' building outline over a
range of scales peaks at 1.000, so the two screenshots are at the same map
zoom, and B4 differs from B5 by a pure translation of (-112, +38) px.

## Accuracy, honestly

- The fit has a **mean 21% residual**. `distance_m` in the survey is
  `walk_seconds × 1.4`, so it is walked distance along a corridor, while the
  fit measures straight lines between dots. Good enough to draw a route on a
  plan; not a substitute for measuring the building.
- Cross-floor agreement is a useful check, since some places exist on both
  floors and should land in the same spot. The Lift agrees to 0.8 m,
  Staircase 1 to 1.2 m, the Main Staircase to 1.5 m, Staircase 3 to 3.7 m.
- **Staircase 2 disagrees by 10 m** (`Hive_B5_E` vs `Hive_B4_D`). That is not
  a tracing error — it is in the source. Whoever drew the node map marked
  Staircase 2 on a different petal of the building on B4 than on B5. One of
  the two dots is on the wrong staircase and only a visit will say which.

## Files

| File | What |
|---|---|
| `hive_B5.png` | 1570×1395, the B5 plan |
| `hive_B4.png` | 1648×1358, the B4 plan |
| `calibration.json` | `origin_x_m`, `origin_y_m`, `metres_per_pixel` per floor |

A viewer turns a node into a point on the image the way the backend
documents it: `pixel = (x_m - origin_x_m) / metres_per_pixel`.

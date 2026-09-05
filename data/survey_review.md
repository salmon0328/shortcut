# Survey review

Written by `scripts/import_survey.py`. Everything below is something the
drawing did not settle on its own. Nothing here has been written to the
survey.

## Links needing a person (7)

These lines run off the edge of the slide towards a label instead of
ending on a place - which is how the cross-floor connectors are drawn -
or carry a time the surveyor left as `?s`.

- page 7: `Hive-B5-I -- ?`, no time (marked stairs)
- page 7: `Hive-B5-F -- ?`, no time (marked Wheelchair)
- page 7: `? -- Hive-B5-F`, no time (marked staircase)
- page 7: `Hive-B5-I -- ?`, no time (marked stairs)
- page 9: `? -- S3-B3-B`, ?s
- page 9: `SS-B3-E -- SS-B3-D`, ?s
- page 9: `SS-B3-A -- ?`, 5s

## Disagreements with the committed survey (1)

- the drawing joins `Hive_B5_A` to `Hive_B5_F` (12s), which the survey does not have. Not added - that floor is already surveyed.

## Places drawn in two spots, so given no position (3)

A crossing is drawn on both buildings' plans, so its end appears twice.
These places route normally; they just do not draw on a plan yet.

- `Hive-SS-Walkway-D`
- `S3-B3-A`
- `SS-B3-F`

## Places nothing connects to the rest (0)

Routing between two places in different groups returns no route at all.
Every group after the first is waiting on a connector from the list above -
the links between floors are the ones the drawing does not settle.

- none, every place can be walked to from every other

## Scale fitted for each floor

- Hive B3: 0.078875 m/pixel from 7 links, worst link off by 28%.
- Hive B4: 0.077500 m/pixel from 12 links, worst link off by 55%.
- Hive B5: 0.058602 m/pixel from 15 links, worst link off by 58%.
- Hive-SS B4: only 2 link(s) join two placed places, too few to trust a scale, so these places get no position.
- S3 B3: only 0 link(s) join two placed places, too few to trust a scale, so these places get no position.
- SS B3: only 1 link(s) join two placed places, too few to trust a scale, so these places get no position.
- SS B3: only 1 link(s) join two placed places, too few to trust a scale, so these places get no position.
- SS B4: only 1 link(s) join two placed places, too few to trust a scale, so these places get no position.

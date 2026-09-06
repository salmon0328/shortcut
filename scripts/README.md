# scripts/

See the top-level [README](../README.md) for what this project is and how to
run it.

Command-line tools. **None of these run inside the server** and none of them
are imported by `src/shortcut/`. Every one either reports what it would do and
stops, or needs `--write` before it changes anything — so running one to find
out what it does is safe.

Each script's own docstring carries its usage; this is the map.

## Still used

| Script | What it does |
|---|---|
| `check_bedrock.py` | Says whether this machine can actually call Claude, and which region and model id work. Four checks, stopping at the first failure. Run it before blaming the code |
| `graduate_overrides.py` | Folds changes out of `data/graph_overrides.json` into the committed survey, for review as a git diff. Refuses to write if the live map would change |
| `import_survey.py` | Reads the drawn node map PDF into the survey, and writes `data/survey_review.md` saying what it did and what it refused to do |
| `upload_photos.py` | Uploads a folder-per-place of surveyed photographs in bulk, instead of a hundred separate page loads |
| `upload_floorplans.py` | Uploads each floor's plan image *and* its calibration, which have to arrive together or the map knows the image exists and still cannot place a pin on it |

## Spent, and kept on purpose

These two have already been run and should not be run again. They are kept
because the damage each repaired is invisible in the data afterwards, and the
next person to see a number that looks odd deserves to find out it was already
found and fixed rather than rediscover it.

| Script | What it repaired |
|---|---|
| `fix_fraction_positions.py` | 24 places whose position had been stored as a fraction of a floorplan where the rest of the project works in metres. A fraction is a perfectly valid number, so nothing failed — every affected place simply drew in the top-left corner of its floor |
| `backfill_from_drawing.py` | Floors left blank for the handful of places that do not say their own floor in their name — the walkway, the canteen, the unnamed Hive doors — once the importer learned to read the floor off the page title instead of a hardcoded page number |

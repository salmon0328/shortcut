"""Put the surveyed Hive floorplans into this machine's floorplan store.

The plans themselves are committed under ``data/floorplan_sources/`` because
they are survey output: they were traced out of the team's node map and they
change only when somebody surveys the building again. Where they *end up*,
``data/floorplans/``, is runtime state and gitignored, like reports and
overrides — so a fresh clone has the plans but not the store, and this script
is the one step that joins the two.

Running it twice is safe. ``FloorplanStore.for_floor`` already treats the most
recent upload for a floor as the live one, so a second run supersedes the first
rather than colliding with it; pass ``--replace`` to delete the older rows
instead of leaving them behind.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shortcut.floorplan_store import FloorplanStore, FloorplanStoreError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCES = PROJECT_ROOT / "data" / "floorplan_sources"
FLOORPLANS = PROJECT_ROOT / "data" / "floorplans"
BUILDING = "Hive"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete any existing plan for the same floor first.",
    )
    args = parser.parse_args()

    calibration = json.loads((SOURCES / "calibration.json").read_text("utf-8"))
    store = FloorplanStore(FLOORPLANS)

    for floor, plan in sorted(calibration.items()):
        image = SOURCES / plan["img"]
        if not image.exists():
            print(f"missing {image}", file=sys.stderr)
            return 1

        if args.replace:
            while (old := store.for_floor(BUILDING, floor)) is not None:
                store.delete(old.id)
                print(f"  removed earlier {floor} plan {old.id}")

        try:
            stored = store.add(
                content=image.read_bytes(),
                content_type="image/png",
                building=BUILDING,
                floor=floor,
                origin_x_m=plan["ox"],
                origin_y_m=plan["oy"],
                metres_per_pixel=plan["mpp"],
                note=(
                    "Traced from the team's Hive node map; scale fitted to the "
                    "surveyed walking distances."
                ),
            )
        except FloorplanStoreError as error:
            print(f"{floor}: {error}", file=sys.stderr)
            return 1

        print(
            f"{BUILDING} {floor}: {stored.id}  "
            f"origin=({stored.origin_x_m}, {stored.origin_y_m}) m  "
            f"{stored.metres_per_pixel:.6f} m/px  "
            f"calibrated={stored.is_calibrated}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

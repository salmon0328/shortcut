"""Convert positions stored as a fraction of a floorplan into metres.

A one-off repair, kept because the damage it fixes is invisible and somebody
will want to see what was done.

**What went wrong.** :mod:`shortcut.nodemap` reads a place's position as a
fraction of the plan it is drawn on - the only form that survives the same
plan being scanned at two resolutions. Every other part of this project works
in metres: the map panel, ``Floorplan.to_pixels``, and every position the
hand-typed survey recorded. The admin import wrote the fraction straight into
the survey, so 24 places carry a number between 0 and 1 where metres are
expected.

Nothing complains, because 0.57 is a perfectly good coordinate. It just draws
every one of those places within a few pixels of the top-left corner of its
floorplan, all on top of each other.

**How they are told apart.** A fraction is between 0 and 1; a real position in
this building is tens of metres. There is no overlap in practice - a place
genuinely less than a metre from its plan's origin would be in the corner of
the building - and any node already outside that range is left alone, so
running this twice changes nothing the second time.

    python scripts/fix_fraction_positions.py            # say what would change
    python scripts/fix_fraction_positions.py --write    # change it
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import pymupdf  # noqa: E402

from shortcut.dotenv import load_dotenv  # noqa: E402
from shortcut.floorplan_store import FloorplanStore  # noqa: E402
from shortcut.survey_import import PlanCalibration  # noqa: E402

SURVEY = PROJECT_ROOT / "data" / "campus_graph.json"
FLOORPLANS = PROJECT_ROOT / "data" / "floorplans"


def looks_like_a_fraction(node: dict) -> bool:
    """Whether this position is a fraction of a plan rather than metres."""
    x, y = node.get("x"), node.get("y")
    if x is None or y is None:
        return False
    return 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Rewrite the survey.")
    arguments = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    floorplans = FloorplanStore(FLOORPLANS)

    calibrations: dict[tuple[str, str], PlanCalibration] = {}
    for plan in floorplans.all():
        if not plan.is_calibrated:
            continue
        image = pymupdf.Pixmap(floorplans.read_file(plan))
        calibrations[(plan.building, plan.floor)] = PlanCalibration(
            origin_x_m=plan.origin_x_m,
            origin_y_m=plan.origin_y_m,
            metres_per_pixel=plan.metres_per_pixel,
            width_px=image.width,
            height_px=image.height,
        )

    survey = json.loads(SURVEY.read_text(encoding="utf-8"))
    fixed: list[str] = []
    unplaceable: list[str] = []

    for node in survey["nodes"]:
        if not looks_like_a_fraction(node):
            continue
        calibration = calibrations.get((node["building"], node["floor"]))
        if calibration is None:
            # No measured plan for that floor, so there is no way to say where
            # this is in metres. Dropping the position is right: an unplaced
            # place is a gap the map reports, and a fraction left in place is
            # a place drawn confidently in the wrong spot.
            unplaceable.append(node["id"])
            node.pop("x", None)
            node.pop("y", None)
            continue
        node["x"], node["y"] = calibration.to_metres((node["x"], node["y"]))
        fixed.append(node["id"])

    print(f"{len(fixed)} place(s) converted from a fraction into metres")
    for node_id in fixed:
        node = next(n for n in survey["nodes"] if n["id"] == node_id)
        print(f"  {node_id:22} -> {node['x']}m, {node['y']}m")

    if unplaceable:
        print(f"\n{len(unplaceable)} place(s) had no measured plan, so lost their position:")
        for node_id in unplaceable:
            print(f"  {node_id}")

    if not fixed and not unplaceable:
        print("Nothing to do: every position is already in metres.")
        return 0

    if not arguments.write:
        print("\nre-run with --write to apply")
        return 0

    SURVEY.write_text(json.dumps(survey, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {SURVEY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fill in what the drawing knows about places already in the survey.

Two gaps, both left by the order things happened in rather than by anything
being wrong with the drawing.

**Floors.** Most places say their floor in their own name - ``Hive-B3-D``. A
handful never did: ``Hive-A``, ``SS-Canteen-B``, ``Hive-SS-Walkway-C``. The
importer used to leave those blank, because the older tool took the floor from
a *hardcoded page number* and that is a guess about one particular file.
Reading it from the page's own title is not a guess - a page headed "Hive + SS
B4" says B4 in as many words - so the importer now does that, and this fills in
the places imported before it did.

**Positions.** A place with no floor has no floorplan, and a place with no
floorplan cannot be put anywhere in metres. So the places above lost their
positions when the survey was repaired. Once they have a floor they can be
placed, which is the second half of the same job.

Neither step invents anything. A place the drawing does not cover, or a floor
with no measured plan, is left exactly as it is and reported.

    python scripts/backfill_from_drawing.py            # say what would change
    python scripts/backfill_from_drawing.py --write    # change it
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
from shortcut.nodemap import node_id_for, read_uploaded_pdf  # noqa: E402
from shortcut.survey_import import PlanCalibration, _building_and_floor  # noqa: E402

SURVEY = PROJECT_ROOT / "data" / "campus_graph.json"
NODE_MAP = PROJECT_ROOT / "data" / "survey_sources" / "hive_node_map.pdf"
FLOORPLANS = PROJECT_ROOT / "data" / "floorplans"


def read_drawing(path: Path) -> dict[str, dict]:
    """What the drawing says about each place: its floor, and where it sits.

    A place drawn on two plans - the crossing between the buildings is drawn
    on both - is left without a position, because "somewhere on one of these
    two images" is not a position. Its floor is still usable.
    """
    found: dict[str, dict] = {}
    twice: set[str] = set()

    for extraction in read_uploaded_pdf(path.read_bytes()):
        if not extraction.floor:
            continue  # an untitled page says nothing about which floor it is
        for place in extraction.places:
            node_id = node_id_for(place.name)
            building, floor = _building_and_floor(place.name, extraction.floor)
            if node_id in found:
                twice.add(node_id)
                continue
            found[node_id] = {
                "building": building,
                "floor": floor,
                "fraction": place.fraction,
            }

    for node_id in twice:
        found[node_id]["fraction"] = None
    return found


def calibrations(floorplans: FloorplanStore) -> dict[tuple[str, str], PlanCalibration]:
    found: dict[tuple[str, str], PlanCalibration] = {}
    for plan in floorplans.all():
        if not plan.is_calibrated:
            continue
        image = pymupdf.Pixmap(floorplans.read_file(plan))
        found[(plan.building, plan.floor)] = PlanCalibration(
            origin_x_m=plan.origin_x_m,
            origin_y_m=plan.origin_y_m,
            metres_per_pixel=plan.metres_per_pixel,
            width_px=image.width,
            height_px=image.height,
        )
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Rewrite the survey.")
    parser.add_argument("--node-map", type=Path, default=NODE_MAP)
    arguments = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    drawing = read_drawing(arguments.node_map)
    scales = calibrations(FloorplanStore(FLOORPLANS))
    survey = json.loads(SURVEY.read_text(encoding="utf-8"))

    floors: list[tuple[str, str]] = []
    placed: list[tuple[str, float, float]] = []
    unknown: list[str] = []
    unplaceable: list[str] = []

    for node in survey["nodes"]:
        drawn = drawing.get(node["id"])
        if drawn is None:
            if not node.get("floor") or node.get("x") is None:
                unknown.append(node["id"])
            continue

        if not node.get("floor") and drawn["floor"]:
            node["floor"] = drawn["floor"]
            floors.append((node["id"], drawn["floor"]))

        if node.get("x") is not None:
            continue

        scale = scales.get((node["building"], node["floor"]))
        if scale is None or drawn["fraction"] is None:
            unplaceable.append(node["id"])
            continue
        node["x"], node["y"] = scale.to_metres(drawn["fraction"])
        placed.append((node["id"], node["x"], node["y"]))

    print(f"{len(floors)} place(s) given a floor from the page they are drawn on")
    for node_id, floor in floors:
        print(f"  {node_id:24} -> {floor}")

    print(f"\n{len(placed)} place(s) given a position")
    for node_id, x, y in placed:
        print(f"  {node_id:24} -> {x}m, {y}m")

    if unplaceable:
        print(f"\n{len(unplaceable)} place(s) still have no position:")
        for node_id in unplaceable:
            print(f"  {node_id}")
    if unknown:
        print(f"\n{len(unknown)} place(s) the drawing does not cover, left alone:")
        for node_id in unknown:
            print(f"  {node_id}")

    if not floors and not placed:
        print("\nNothing to do.")
        return 0
    if not arguments.write:
        print("\nre-run with --write to apply")
        return 0

    SURVEY.write_text(json.dumps(survey, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {SURVEY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

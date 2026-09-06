"""Put the floorplans where the app can draw them, already calibrated.

    .venv/bin/python scripts/upload_floorplans.py                   # say what it would do
    .venv/bin/python scripts/upload_floorplans.py --write
    .venv/bin/python scripts/upload_floorplans.py --write --plans-dir ~/Downloads/Maps/Original

Uploading a plan is only half of it. A plan nobody has calibrated is a
picture: the app knows the image exists and still cannot say where on it a
place is, which is the state ``mapView.js`` reports as "set a scale and origin
in admin mode" - a screen that does not exist. So this does both, and takes
the calibration from the same fit that gave the places their positions, which
is the only way the two can agree.

The plans come out of the node map itself by default, so this needs nothing
but the repository. ``--plans-dir`` takes the team's higher-resolution scans
instead: same crop, more pixels, and because a place's position is stored as a
fraction of the plan rather than as a pixel, swapping one for the other is
just a different number in the calibration.

Where the files land is not this script's decision. ``SHORTCUT_S3_BUCKET``
sends them to the shared bucket, and its absence keeps them on this machine -
the same switch the running app obeys, so what you see locally is what your
teammates get.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import import_survey  # noqa: E402
import nodemap  # noqa: E402
from shortcut.dotenv import load_dotenv  # noqa: E402
from shortcut.floorplan_store import FloorplanStore  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FLOORPLANS = PROJECT_ROOT / "data" / "floorplans"

#: The team's own scans, named by floor. Same crop as the plans embedded in
#: the node map - checked to four decimal places on the aspect ratio - so a
#: position read off one is true of the other.
SCAN_NAME = "Hive-{floor}.png"


def plan_bytes_from_pdf(path: Path, page_number: int, xref: int) -> tuple[bytes, str]:
    """The floorplan image as it is embedded in the node map."""
    with pymupdf.open(path) as document:
        extracted = document.extract_image(xref)
    return extracted["image"], f"image/{extracted['ext']}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="upload again even for a floor that already has a calibrated plan",
    )
    parser.add_argument(
        "--plans-dir",
        type=Path,
        default=None,
        help="use higher-resolution scans from here instead of the node map's own",
    )
    parser.add_argument("--node-map", type=Path, default=import_survey.NODE_MAP)
    arguments = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    where = os.environ.get("SHORTCUT_S3_BUCKET")
    print(f"uploading to {'s3://' + where if where else 'local disk, ' + str(FLOORPLANS)}")

    pages = nodemap.read_node_map(arguments.node_map)
    survey = import_survey.load_survey()
    floors = {
        node["id"]: (node["building"], node["floor"]) for node in survey["nodes"]
    }
    positions = import_survey.home_plans(pages)
    pairs = {
        frozenset((edge["from"], edge["to"])): edge["walk_seconds"]
        for edge in survey["edges"]
    }
    _, scales, _ = import_survey.fit_positions(positions, floors, pairs, set(floors))

    store = FloorplanStore(FLOORPLANS)
    already = {(plan.building, plan.floor) for plan in store.all()}

    for (building, floor), scale in sorted(scales.items()):
        plan = next(
            (
                place.plan
                for name, place in positions.items()
                if floors.get(nodemap.node_id_for(name)) == (building, floor)
                and place.plan is not None
            ),
            None,
        )
        if plan is None:
            continue

        source = "the node map"
        content_type = "image/png"
        if arguments.plans_dir is not None:
            scan = arguments.plans_dir.expanduser() / SCAN_NAME.format(floor=floor)
            if scan.exists():
                content, width = scan.read_bytes(), _png_width(scan)
                source = str(scan)
            else:
                print(f"  {building} {floor}: no {scan.name}, falling back to the node map")
                content, content_type = plan_bytes_from_pdf(
                    arguments.node_map, 0, plan.xref
                )
                width = plan.width
        else:
            content, content_type = plan_bytes_from_pdf(arguments.node_map, 0, plan.xref)
            width = plan.width

        # A position was fitted against the plan as the node map holds it. Any
        # other copy of the same crop is that one scaled, so the metres each
        # pixel covers scales the other way. Getting this backwards puts every
        # pin in the right pattern and the wrong place.
        metres_per_pixel = scale * (plan.width / width)

        # A floor that already has a plan is left alone unless asked. The
        # store keeps every upload and serves the newest, so running this
        # twice without the check quietly doubles the plans rather than
        # replacing them - which still draws, and is why it went unnoticed.
        if (building, floor) in already and not arguments.replace:
            print(f"  {building} {floor}: already has a calibrated plan, left alone")
            continue

        print(
            f"  {building} {floor}: {len(content) / 1e6:.2f} MB from {source}, "
            f"{width}px wide, {metres_per_pixel:.6f} m/pixel "
            f"({width * metres_per_pixel:.0f} m across)"
        )

        if not arguments.write:
            continue

        # Calibrated on the way in rather than in a second call: a plan that
        # exists uncalibrated is one the map can show and cannot draw on, and
        # there is no screen anywhere for fixing that.
        store.add(
            building=building,
            floor=floor,
            content=content,
            content_type=content_type,
            origin_x_m=0.0,
            origin_y_m=0.0,
            metres_per_pixel=metres_per_pixel,
            note=f"Traced from {source}; scale fitted across every link on this floor.",
        )

    if not arguments.write:
        print("\nre-run with --write to upload")
    return 0


def _png_width(path: Path) -> int:
    """A PNG's width, read from its header rather than by decoding it."""
    header = path.read_bytes()[:24]
    return int.from_bytes(header[16:20], "big")


if __name__ == "__main__":
    raise SystemExit(main())

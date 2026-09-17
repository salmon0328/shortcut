"""Put one floor on the globe, from a few points whose position is known.

    # places, by latitude/longitude read off OneMap, OSM or the app's ?geo-debug map
    .venv/bin/python scripts/georef_floor.py Hive B5 \\
        --point Hive_B5_I=1.34330,103.68281 --point Hive_B5_C=1.34305,103.68253

    # places that stand directly above/below the same places on a floor already done
    .venv/bin/python scripts/georef_floor.py Hive B3 --stack-over B4

    # the floorplan image instead, by pixel
    .venv/bin/python scripts/georef_floor.py Hive B5 --plan-pixel 1210,640=1.34330,103.68281 ...

Fits the rotation, position and scale that take the floor's drawing onto the
points given, prints how far each point lands from where it should (in
metres), and with ``--write`` saves the result into ``data/campus_geo.json``.
Two points fix a fit exactly and so say nothing about its quality; three or
more are worth the trouble, because then the residuals mean something.

``--stack-over`` matches places by *name* - "Staircase 1" on B3 over
"Staircase 1" on B4 - which is right for stairwells and lift shafts and wrong
for anything else, so only stairs, lifts and lift lobbies are used.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shortcut.geo import GeoError, fit_transform, load_campus_geo  # noqa: E402
from shortcut.graph_store import load_graph  # noqa: E402
from shortcut.overrides import apply_overrides, load_overrides  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GEO_PATH = PROJECT_ROOT / "data" / "campus_geo.json"
BUILDINGS_PATH = PROJECT_ROOT / "data" / "campus_buildings.geojson"
GRAPH_PATH = PROJECT_ROOT / "data" / "campus_graph.json"
OVERRIDES_PATH = PROJECT_ROOT / "data" / "graph_overrides.json"

#: Kinds of place that sit in a vertical shaft, and so stack across floors.
STACKING_TYPES = {"stairs", "lift"}
STACKING_NAMES = ("staircase", "lift")


def _latlon(text: str) -> tuple[float, float]:
    lat, lon = (float(part) for part in text.split(","))
    return lat, lon


def _pair(text: str) -> tuple[str, tuple[float, float]]:
    key, _, where = text.partition("=")
    if not where:
        raise argparse.ArgumentTypeError(f"expected KEY=LAT,LON, got {text!r}")
    return key.strip(), _latlon(where)


def _stacks(node) -> bool:
    return node.type in STACKING_TYPES or node.name.lower().startswith(STACKING_NAMES)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("building")
    parser.add_argument("floor")
    parser.add_argument("--point", action="append", type=_pair, default=[],
                        help="NODE_ID=LAT,LON")
    parser.add_argument("--stack-over", metavar="FLOOR",
                        help="use stairs and lifts above/below this floor's")
    parser.add_argument("--plan-pixel", action="append", type=_pair, default=[],
                        help="U,V=LAT,LON on the floorplan image")
    parser.add_argument("--floorplan-id",
                        help="which floorplan the pixels are on (kept if already set)")
    parser.add_argument("--write", action="store_true")
    arguments = parser.parse_args()

    geo = load_campus_geo(GEO_PATH, BUILDINGS_PATH)
    graph = apply_overrides(load_graph(GRAPH_PATH), load_overrides(OVERRIDES_PATH))
    on_floor = [
        node
        for node in graph.nodes.values()
        if node.building == arguments.building and node.floor == arguments.floor
    ]

    labelled: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    fitting_plan = bool(arguments.plan_pixel)

    if fitting_plan:
        if arguments.point or arguments.stack_over:
            parser.error("--plan-pixel fits the image; do not mix it with place points")
        for key, target in arguments.plan_pixel:
            u, v = (float(part) for part in key.split(","))
            labelled.append((f"pixel {key}", (u, v), target))
    else:
        by_id = {node.id: node for node in on_floor}
        for node_id, target in arguments.point:
            node = by_id.get(node_id)
            if node is None:
                parser.error(f"{node_id} is not a place on {arguments.building} {arguments.floor}")
            if node.x is None or node.y is None:
                parser.error(f"{node_id} has no x/y to fit from")
            labelled.append((f"{node.id} {node.name}", (node.x, node.y), target))

        if arguments.stack_over:
            below = {
                node.name: node
                for node in graph.nodes.values()
                if node.building == arguments.building
                and node.floor == arguments.stack_over
                and _stacks(node)
            }
            for node in on_floor:
                other = below.get(node.name)
                if other is None or not _stacks(node) or node.x is None:
                    continue
                placed = geo.position(other)
                if placed is None:
                    continue
                lon, lat, _ = placed
                labelled.append((f"{node.id} {node.name} over {other.id}", (node.x, node.y), (lat, lon)))

    if len(labelled) < 2:
        parser.error("need at least two control points")

    try:
        transform, residuals = fit_transform((drawn, target) for _, drawn, target in labelled)
    except GeoError as error:
        parser.error(str(error))

    what = "floorplan image" if fitting_plan else "places"
    print(f"{arguments.building} {arguments.floor}, {what}, from {len(labelled)} point(s)\n")
    for (label, _, _), miss in zip(labelled, residuals):
        print(f"  {miss:6.2f} m  {label}")
    print(
        f"\n  rotation {transform.rotation_deg:.3f} deg, scale {transform.scale:.6f}, "
        f"origin {transform.origin_lat:.8f}, {transform.origin_lon:.8f}"
    )
    if len(labelled) == 2:
        print("  (two points always fit exactly; add a third to learn how good this is)")

    if not arguments.write:
        print("\nDry run. Add --write to save it into", GEO_PATH.relative_to(PROJECT_ROOT))
        return 0

    raw = json.loads(GEO_PATH.read_text(encoding="utf-8"))
    building = next((b for b in raw["buildings"] if b["name"] == arguments.building), None)
    if building is None:
        building = {"name": arguments.building, "height_m": 20, "floors": []}
        raw["buildings"].append(building)
    level = next((f for f in building.setdefault("floors", []) if f["floor"] == arguments.floor), None)
    if level is None:
        level = {"floor": arguments.floor, "elevation_m": 0}
        building["floors"].append(level)

    rounded = {
        "origin_lat": round(transform.origin_lat, 8),
        "origin_lon": round(transform.origin_lon, 8),
        "rotation_deg": round(transform.rotation_deg, 3),
        "scale": round(transform.scale, 6),
    }
    if fitting_plan:
        plan = level.setdefault("plan", {})
        plan_id = arguments.floorplan_id or plan.get("floorplan_id")
        if not plan_id:
            parser.error("say which floorplan these pixels are on with --floorplan-id")
        plan["floorplan_id"] = plan_id
        plan["transform"] = rounded
    else:
        level["transform"] = rounded

    GEO_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("\nwrote", GEO_PATH.relative_to(PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())

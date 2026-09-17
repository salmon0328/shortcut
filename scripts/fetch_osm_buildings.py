"""Fetch building outlines from OpenStreetMap for the 3D map.

    .venv/bin/python scripts/fetch_osm_buildings.py            # say what it would write
    .venv/bin/python scripts/fetch_osm_buildings.py --write

Reads the OSM way id of every building in ``data/campus_geo.json``, asks the
public Overpass API once for those ways *and* every other building on campus,
and writes their outlines to ``data/campus_buildings.geojson``.

The rest of campus is there as scenery. The base map's own 3D buildings are
switched off, because its tiles merge neighbouring buildings of the same
height into one shape and so cannot have ours taken out of them; drawing the
whole campus from one source keeps a building from being drawn twice. The server reads that file; it never calls
Overpass itself, so the map works offline and the public service is asked
once per change rather than once per visitor.

Outlines are only half a building. OSM has no heights for the NTU buildings
this project routes through, so theirs live in the geo file and are set by
hand. Scenery takes ``height``, else ``building:levels`` at 3.5 m a storey,
else a flat default.

The data is (c) OpenStreetMap contributors under the ODbL. The written file
says so, and the map shows the credit wherever the outlines are drawn.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GEO_PATH = PROJECT_ROOT / "data" / "campus_geo.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "campus_buildings.geojson"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Overpass asks that callers say who they are.
USER_AGENT = "shortcut-ntu-navigation/0.1 (student prototype)"
#: The NTU campus, south-west to north-east.
CAMPUS_BBOX = (1.3380, 103.6700, 1.3580, 103.6930)
STOREY_M = 3.5
DEFAULT_HEIGHT_M = 10.0
ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"


def wanted_ways(geo_path: Path) -> dict[int, str]:
    """OSM way id -> building name, from the geo file."""
    raw = json.loads(geo_path.read_text(encoding="utf-8"))
    return {
        building["osm_way"]: building["name"]
        for building in raw.get("buildings", [])
        if isinstance(building.get("osm_way"), int)
    }


def _tls_context() -> ssl.SSLContext:
    """Verified TLS, even on a python.org macOS build with no CA bundle.

    Those builds trust nothing until "Install Certificates" has been run, so
    certifi's bundle is used when it is installed (boto3 brings it along).
    """
    try:
        import certifi  # noqa: PLC0415 - optional
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def fetch_ways(way_ids: list[int]) -> list[dict]:
    south, west, north, east = CAMPUS_BBOX
    query = (
        "[out:json][timeout:60];("
        f"way(id:{','.join(map(str, way_ids))});"
        f'way["building"]({south},{west},{north},{east});'
        ");out geom;"
    )
    body = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(
        OVERPASS_URL, data=body, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=60, context=_tls_context()) as response:
        return json.load(response)["elements"]


def scenery_height(tags: dict) -> float:
    """How tall to draw a building nobody has measured for this project."""
    # A zero is somebody's placeholder, not a measurement.
    for key in ("height", "building:height"):
        try:
            height = float(tags[key].split()[0])
        except (KeyError, ValueError, IndexError):
            continue
        if height > 0:
            return round(height, 1)
    try:
        levels = float(tags["building:levels"])
    except (KeyError, ValueError):
        levels = 0
    return round(levels * STOREY_M, 1) if levels > 0 else DEFAULT_HEIGHT_M


def as_geojson(elements: list[dict], names: dict[int, str]) -> dict:
    features = []
    for element in sorted(elements, key=lambda item: item["id"]):
        ring = [
            [round(point["lon"], 7), round(point["lat"], 7)]
            for point in element.get("geometry", [])
        ]
        if len(ring) < 4 or element.get("type") != "way":
            continue
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        tags = element.get("tags", {})
        properties = {
            "osm_way": element["id"],
            "osm_name": tags.get("name", ""),
        }
        if element["id"] in names:
            properties["building"] = names[element["id"]]
        else:
            properties["height_m"] = scenery_height(tags)
            properties["min_height_m"] = (
                round(float(tags["building:min_level"]) * STOREY_M, 1)
                if tags.get("building:min_level", "").replace(".", "", 1).isdigit()
                else 0.0
            )
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    return {"type": "FeatureCollection", "attribution": ATTRIBUTION, "features": features}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the GeoJSON file")
    arguments = parser.parse_args()

    names = wanted_ways(GEO_PATH)
    if not names:
        print(f"No building in {GEO_PATH} names an osm_way; nothing to fetch.")
        return 0

    print(f"asking Overpass for {len(names)} named way(s) and the campus around them")
    elements = fetch_ways(sorted(names))
    collection = as_geojson(elements, names)

    found = {feature["properties"]["osm_way"] for feature in collection["features"]}
    for way, name in sorted(names.items()):
        mark = "ok     " if way in found else "MISSING"
        print(f"  {mark} {name:<10} way {way}")
    print(f"  plus {len(found - set(names))} scenery building(s)")

    if not arguments.write:
        print("\nDry run. Add --write to save", OUTPUT_PATH.relative_to(PROJECT_ROOT))
        return 0

    # One feature per line: small enough to read a diff of, without
    # spreading every coordinate over a line of its own.
    lines = ",\n".join(json.dumps(feature, ensure_ascii=False) for feature in collection["features"])
    OUTPUT_PATH.write_text(
        '{"type": "FeatureCollection",\n'
        f'"attribution": {json.dumps(collection["attribution"], ensure_ascii=False)},\n'
        f'"features": [\n{lines}\n]}}\n',
        encoding="utf-8",
    )
    print("wrote", OUTPUT_PATH.relative_to(PROJECT_ROOT))
    return 0 if set(names) <= found else 1


if __name__ == "__main__":
    sys.exit(main())

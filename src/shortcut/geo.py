"""Where the campus sits on the globe, for the 3D map.

Nothing here is used for routing. The graph's ``x``/``y`` are metres measured
on each floor's own drawing, and a floorplan's calibration only links those
metres to the image's pixels; neither says where on Earth anything is. This
module holds the missing link, read from two version-controlled files:

* ``data/campus_geo.json`` - hand-edited. Per building: its OpenStreetMap way,
  a height, and per floor an elevation plus two transforms:

  - ``transform`` takes a place's ``x``/``y`` to latitude/longitude;
  - ``plan.transform`` takes a floorplan's *pixels* to latitude/longitude.

  They are separate on purpose. A floor's drawing and its surveyed places were
  measured separately, and on some floors they disagree by metres; fitting
  each to the real world on its own evidence keeps one's error out of the
  other.
* ``data/campus_buildings.geojson`` - building outlines, fetched from
  OpenStreetMap by ``scripts/fetch_osm_buildings.py``.

A transform is the simplest thing that fits a drawing to the ground: an
origin, a rotation and a scale (a *similarity*). Both drawing axes run the
way an image does - ``u`` to the right, ``v`` down - so a rotation of 0 means
the drawing is north-up, and a positive rotation turns it clockwise.

At the size of a campus the Earth is flat to well under a centimetre, so
metres become degrees with the equirectangular approximation. The browser
repeats that arithmetic for floorplan corners (``web/src/map3d.js``); change
one and change the other.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shortcut.graph_store import CampusGraph, Node

__all__ = [
    "BuildingGeo",
    "CampusGeo",
    "FloorGeo",
    "GeoError",
    "PlanGeo",
    "Transform",
    "fit_transform",
    "load_campus_geo",
    "load_scenery",
]

# WGS84's equatorial radius. A sphere is plenty at this scale.
EARTH_RADIUS_M = 6378137.0
METRES_PER_DEGREE = math.pi * EARTH_RADIUS_M / 180.0


class GeoError(ValueError):
    """The geo files are present but cannot be used as written."""


@dataclass(frozen=True)
class Transform:
    """Drawing units (``u`` right, ``v`` down) to latitude/longitude."""

    origin_lat: float
    origin_lon: float
    rotation_deg: float = 0.0
    scale: float = 1.0

    def to_metres(self, u: float, v: float) -> tuple[float, float]:
        """East and north of the origin, in metres."""
        angle = math.radians(self.rotation_deg)
        cos, sin = math.cos(angle), math.sin(angle)
        east = self.scale * (cos * u - sin * v)
        south = self.scale * (sin * u + cos * v)
        return east, -south

    def apply(self, u: float, v: float) -> tuple[float, float]:
        """``(lon, lat)`` of a point on the drawing - GeoJSON's order."""
        east, north = self.to_metres(u, v)
        lat = self.origin_lat + north / METRES_PER_DEGREE
        lon = self.origin_lon + east / (
            METRES_PER_DEGREE * math.cos(math.radians(self.origin_lat))
        )
        return lon, lat

    def as_dict(self) -> dict[str, float]:
        return {
            "origin_lat": self.origin_lat,
            "origin_lon": self.origin_lon,
            "rotation_deg": self.rotation_deg,
            "scale": self.scale,
        }


@dataclass(frozen=True)
class PlanGeo:
    """Where one floorplan image lies: its pixels to the globe."""

    floorplan_id: str
    transform: Transform


@dataclass(frozen=True)
class FloorGeo:
    building: str
    floor: str
    # Height of the floor above the building's ground, in metres.
    elevation_m: float
    transform: Transform | None = None
    plan: PlanGeo | None = None
    note: str = ""


@dataclass(frozen=True)
class BuildingGeo:
    name: str
    height_m: float
    osm_way: int | None = None
    floors: tuple[FloorGeo, ...] = ()
    # Outer ring as [lon, lat] pairs, from the OSM file. None when the file
    # has no outline for this building yet.
    footprint: tuple[tuple[float, float], ...] | None = None


@dataclass(frozen=True)
class CampusGeo:
    buildings: tuple[BuildingGeo, ...] = ()
    # Places pinned directly, for those with no floor coordinates - an
    # outdoor crossing, a building nobody has drawn yet. node id -> (lat, lon).
    places: dict[str, tuple[float, float]] = field(default_factory=dict)
    attribution: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.buildings and not self.places

    def floor(self, building: str, floor: str) -> FloorGeo | None:
        for entry in self.buildings:
            if entry.name == building:
                for level in entry.floors:
                    if level.floor == floor:
                        return level
        return None

    def position(self, node: Node) -> tuple[float, float, float] | None:
        """``(lon, lat, elevation_m)`` of a place, or None if it cannot be placed.

        A pinned position wins over the floor's transform, so a place whose
        drawn coordinates are known to be wrong can be corrected by hand.
        """
        level = self.floor(node.building, node.floor)
        elevation = level.elevation_m if level else 0.0

        pinned = self.places.get(node.id)
        if pinned is not None:
            lat, lon = pinned
            return lon, lat, elevation

        if level is None or level.transform is None:
            return None
        if node.x is None or node.y is None:
            return None
        lon, lat = level.transform.apply(node.x, node.y)
        return lon, lat, elevation

    def positions(self, graph: CampusGraph) -> dict[str, tuple[float, float, float]]:
        placed = {}
        for node in graph.nodes.values():
            at = self.position(node)
            if at is not None:
                placed[node.id] = at
        return placed


# --------------------------------------------------------------------------
# Reading the files
# --------------------------------------------------------------------------


def load_campus_geo(geo_path: Path, footprints_path: Path | None = None) -> CampusGeo:
    """Read the geo file, and outlines if there are any.

    A missing geo file is not an error: it means nobody has put the campus on
    the map yet, and the app carries on with flat floorplans. A file that is
    there but wrong *is* an error, because a silently skipped floor would look
    exactly like one nobody had placed.
    """
    if not geo_path.exists():
        return CampusGeo()

    try:
        raw = json.loads(geo_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise GeoError(f"{geo_path} is not valid JSON: {error.msg}") from error

    footprints = _read_footprints(footprints_path) if footprints_path else {}
    if not isinstance(raw, dict):
        raise GeoError(f"{geo_path} must hold a JSON object.")

    buildings = tuple(
        _parse_building(entry, index, footprints)
        for index, entry in enumerate(_list(raw.get("buildings", []), "buildings"))
    )
    names = [building.name for building in buildings]
    if len(set(names)) != len(names):
        raise GeoError("Each building may appear only once in the geo file.")

    places = {}
    for node_id, entry in _dict(raw.get("places", {}), "places").items():
        where = f"places.{node_id}"
        places[node_id] = (
            _latitude(entry, "lat", where),
            _longitude(entry, "lon", where),
        )

    return CampusGeo(
        buildings=buildings,
        places=places,
        attribution=str(raw.get("attribution", "")),
    )


def load_scenery(path: Path) -> dict:
    """The rest of campus, as GeoJSON for the map to draw as-is.

    Every building in the outlines file that the geo file does not name. Each
    carries ``height_m`` and ``min_height_m``; they are drawn and nothing else.
    """
    empty = {"type": "FeatureCollection", "features": []}
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise GeoError(f"{path} is not valid JSON: {error.msg}") from error
    features = [
        feature
        for feature in _list(raw.get("features", []), "features")
        if "building" not in feature.get("properties", {})
        and isinstance(feature.get("properties", {}).get("height_m"), int | float)
    ]
    return {**empty, "attribution": raw.get("attribution", ""), "features": features}


def _read_footprints(path: Path) -> dict[int, tuple[tuple[float, float], ...]]:
    """OSM way id -> outer ring, from the GeoJSON the fetch script writes."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise GeoError(f"{path} is not valid JSON: {error.msg}") from error

    rings = {}
    for feature in _list(raw.get("features", []), "features"):
        way = feature.get("properties", {}).get("osm_way")
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Polygon" or not isinstance(way, int):
            continue
        rings[way] = tuple((float(lon), float(lat)) for lon, lat in geometry["coordinates"][0])
    return rings


def _parse_building(
    entry: Any, index: int, footprints: dict[int, tuple[tuple[float, float], ...]]
) -> BuildingGeo:
    where = f"buildings[{index}]"
    entry = _dict(entry, where)
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise GeoError(f"{where} needs a name matching the graph's building.")

    osm_way = entry.get("osm_way")
    if osm_way is not None and not isinstance(osm_way, int):
        raise GeoError(f"{where}.osm_way must be an OpenStreetMap way id.")

    floors = tuple(
        _parse_floor(name, level, f"{where}.floors[{number}]")
        for number, level in enumerate(_list(entry.get("floors", []), f"{where}.floors"))
    )
    labels = [level.floor for level in floors]
    if len(set(labels)) != len(labels):
        raise GeoError(f"{where} lists a floor twice.")

    return BuildingGeo(
        name=name,
        height_m=_positive(entry, "height_m", where),
        osm_way=osm_way,
        floors=floors,
        footprint=footprints.get(osm_way) if osm_way is not None else None,
    )


def _parse_floor(building: str, entry: Any, where: str) -> FloorGeo:
    entry = _dict(entry, where)
    floor = entry.get("floor")
    if not isinstance(floor, str) or not floor:
        raise GeoError(f"{where} needs a floor label.")

    elevation = entry.get("elevation_m", 0.0)
    if not isinstance(elevation, int | float) or isinstance(elevation, bool):
        raise GeoError(f"{where}.elevation_m must be a number.")

    transform = None
    if entry.get("transform") is not None:
        transform = _parse_transform(entry["transform"], f"{where}.transform")

    plan = None
    if entry.get("plan") is not None:
        plan_entry = _dict(entry["plan"], f"{where}.plan")
        plan_id = plan_entry.get("floorplan_id")
        if not isinstance(plan_id, str) or not plan_id:
            raise GeoError(f"{where}.plan needs the floorplan_id it was fitted to.")
        plan = PlanGeo(
            floorplan_id=plan_id,
            transform=_parse_transform(plan_entry.get("transform"), f"{where}.plan.transform"),
        )

    return FloorGeo(
        building=building,
        floor=floor,
        elevation_m=float(elevation),
        transform=transform,
        plan=plan,
        note=str(entry.get("note", "")),
    )


def _parse_transform(entry: Any, where: str) -> Transform:
    entry = _dict(entry, where)
    rotation = entry.get("rotation_deg", 0.0)
    if not isinstance(rotation, int | float) or isinstance(rotation, bool):
        raise GeoError(f"{where}.rotation_deg must be a number.")
    return Transform(
        origin_lat=_latitude(entry, "origin_lat", where),
        origin_lon=_longitude(entry, "origin_lon", where),
        rotation_deg=float(rotation),
        scale=_positive(entry, "scale", where),
    )


def _dict(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise GeoError(f"{where} must be a JSON object.")
    return value


def _list(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise GeoError(f"{where} must be a JSON list.")
    return value


def _number(entry: dict, key: str, where: str) -> float:
    value = entry.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise GeoError(f"{where}.{key} must be a number.")
    return float(value)


def _positive(entry: dict, key: str, where: str) -> float:
    value = _number(entry, key, where)
    if value <= 0:
        raise GeoError(f"{where}.{key} must be greater than zero.")
    return value


def _latitude(entry: Any, key: str, where: str) -> float:
    value = _number(_dict(entry, where), key, where)
    if not -90 <= value <= 90:
        raise GeoError(f"{where}.{key} is not a latitude.")
    return value


def _longitude(entry: Any, key: str, where: str) -> float:
    value = _number(_dict(entry, where), key, where)
    if not -180 <= value <= 180:
        raise GeoError(f"{where}.{key} is not a longitude.")
    return value


# --------------------------------------------------------------------------
# Fitting a transform to control points
# --------------------------------------------------------------------------


def fit_transform(
    pairs: Iterable[tuple[tuple[float, float], tuple[float, float]]],
) -> tuple[Transform, list[float]]:
    """The similarity that best takes drawing points onto known positions.

    ``pairs`` are ``((u, v), (lat, lon))``. Returns the transform and, for
    each pair, how far the fitted position lands from the known one, in
    metres - the number that says whether the fit can be trusted.

    Least squares in the complex plane: with ``z = u + iv`` and ``w`` the
    target in local east/south metres, ``w = a·z + b`` is linear in ``a`` and
    ``b``, and ``a`` carries both the rotation and the scale.
    """
    pairs = list(pairs)
    if len(pairs) < 2:
        raise GeoError("A fit needs at least two control points.")

    lat0 = sum(lat for _, (lat, _lon) in pairs) / len(pairs)
    lon0 = sum(lon for _, (_lat, lon) in pairs) / len(pairs)
    per_lon = METRES_PER_DEGREE * math.cos(math.radians(lat0))

    zs = [complex(u, v) for (u, v), _ in pairs]
    ws = [
        complex((lon - lon0) * per_lon, -(lat - lat0) * METRES_PER_DEGREE)
        for _, (lat, lon) in pairs
    ]

    count = len(pairs)
    z_mean = sum(zs) / count
    w_mean = sum(ws) / count
    spread = sum(abs(z - z_mean) ** 2 for z in zs)
    if spread == 0:
        raise GeoError("The control points are all the same drawing point.")

    a = sum((w - w_mean) * (z - z_mean).conjugate() for z, w in zip(zs, ws)) / spread
    b = w_mean - a * z_mean
    if a == 0:
        raise GeoError("The control points are all the same place.")

    residuals = [abs(a * z + b - w) for z, w in zip(zs, ws)]

    # b is where the drawing's (0, 0) lands, in the local frame.
    origin_lat = lat0 - b.imag / METRES_PER_DEGREE
    origin_lon = lon0 + b.real / per_lon
    transform = Transform(
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        rotation_deg=math.degrees(math.atan2(a.imag, a.real)),
        scale=abs(a),
    )
    return transform, residuals

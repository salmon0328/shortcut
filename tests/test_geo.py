"""Tests for putting the campus on the globe.

The arithmetic is checked against positions worked out by hand, the fit
against a transform it has to recover, and the files against the ways they
can be wrong. Nothing here touches routing.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import shortcut.api as api
from shortcut.geo import (
    METRES_PER_DEGREE,
    CampusGeo,
    GeoError,
    Transform,
    fit_transform,
    load_campus_geo,
)
from shortcut.graph_store import CampusGraph, Node

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NTU = (1.3432, 103.6827)


def _metres_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance between two (lon, lat) points, flat-Earth."""
    per_lon = METRES_PER_DEGREE * math.cos(math.radians(a[1]))
    return math.hypot((a[0] - b[0]) * per_lon, (a[1] - b[1]) * METRES_PER_DEGREE)


def _write(path: Path, content: object) -> Path:
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def _geo_file(tmp_path: Path, **building_overrides) -> Path:
    building = {
        "name": "Hive",
        "osm_way": 1,
        "height_m": 30,
        "floors": [
            {
                "floor": "B5",
                "elevation_m": 0,
                "transform": {"origin_lat": NTU[0], "origin_lon": NTU[1], "rotation_deg": 0, "scale": 1},
                "plan": {
                    "floorplan_id": "plan-1",
                    "transform": {"origin_lat": NTU[0], "origin_lon": NTU[1], "rotation_deg": 0, "scale": 0.03},
                },
            },
            {"floor": "B4", "elevation_m": 4.5},
        ],
    }
    building.update(building_overrides)
    return _write(tmp_path / "campus_geo.json", {"attribution": "test", "buildings": [building]})


# --------------------------------------------------------------------------
# The transform
# --------------------------------------------------------------------------


def test_origin_maps_to_itself():
    transform = Transform(*NTU, rotation_deg=37, scale=2.5)
    assert transform.apply(0, 0) == pytest.approx((NTU[1], NTU[0]))


def test_unrotated_axes_point_east_and_south():
    transform = Transform(*NTU)
    east = transform.apply(10, 0)
    south = transform.apply(0, 10)

    assert east[0] > NTU[1] and east[1] == pytest.approx(NTU[0])
    assert south[1] < NTU[0] and south[0] == pytest.approx(NTU[1])
    assert _metres_between(east, (NTU[1], NTU[0])) == pytest.approx(10, abs=1e-6)


def test_positive_rotation_turns_clockwise():
    # A drawing turned 90 degrees clockwise has its right-hand axis pointing
    # south: what was "east on the page" is south on the ground.
    transform = Transform(*NTU, rotation_deg=90)
    lon, lat = transform.apply(10, 0)
    assert lon == pytest.approx(NTU[1])
    assert (NTU[0] - lat) * METRES_PER_DEGREE == pytest.approx(10, abs=1e-6)


def test_scale_multiplies_distances():
    transform = Transform(*NTU, rotation_deg=12, scale=0.03)
    corner = transform.apply(1000, 0)
    assert _metres_between(corner, (NTU[1], NTU[0])) == pytest.approx(30, abs=1e-6)


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------


def test_fit_recovers_a_known_transform():
    truth = Transform(1.3435, 103.6824, rotation_deg=-23.5, scale=1.37)
    drawn = [(0, 0), (40, 3), (12, 55), (60, 60)]
    pairs = [((u, v), truth.apply(u, v)[::-1]) for u, v in drawn]

    fitted, residuals = fit_transform(pairs)

    assert fitted.rotation_deg == pytest.approx(truth.rotation_deg, abs=1e-6)
    assert fitted.scale == pytest.approx(truth.scale, rel=1e-6)
    assert fitted.origin_lat == pytest.approx(truth.origin_lat, abs=1e-9)
    assert fitted.origin_lon == pytest.approx(truth.origin_lon, abs=1e-9)
    assert max(residuals) < 1e-3


def test_fit_reports_how_far_off_a_bad_point_is():
    truth = Transform(*NTU, rotation_deg=5, scale=1)
    pairs = [((u, v), truth.apply(u, v)[::-1]) for u, v in [(0, 0), (50, 0), (0, 50), (50, 50)]]
    # Move one known position 4 m north.
    (drawn, (lat, lon)) = pairs[3]
    pairs[3] = (drawn, (lat + 4 / METRES_PER_DEGREE, lon))

    _, residuals = fit_transform(pairs)

    assert residuals[3] == max(residuals)
    assert 1 < residuals[3] < 4


@pytest.mark.parametrize(
    "pairs",
    [
        [((0, 0), NTU)],
        [((1, 1), NTU), ((1, 1), (1.34, 103.68))],
        [((0, 0), NTU), ((5, 5), NTU)],
    ],
    ids=["one point", "same drawing point", "same place"],
)
def test_fit_refuses_what_cannot_be_fitted(pairs):
    with pytest.raises(GeoError):
        fit_transform(pairs)


# --------------------------------------------------------------------------
# Reading the files
# --------------------------------------------------------------------------


def test_missing_file_means_nothing_placed(tmp_path: Path):
    geo = load_campus_geo(tmp_path / "absent.json")
    assert geo.is_empty


def test_footprints_join_by_osm_way(tmp_path: Path):
    ring = [[103.68, 1.34], [103.69, 1.34], [103.69, 1.35], [103.68, 1.34]]
    outlines = _write(
        tmp_path / "buildings.geojson",
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"osm_way": 1},
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                }
            ],
        },
    )
    geo = load_campus_geo(_geo_file(tmp_path), outlines)
    assert geo.buildings[0].footprint == tuple(tuple(point) for point in ring)


@pytest.mark.parametrize(
    "change, message",
    [
        ({"height_m": 0}, "height_m"),
        ({"osm_way": "389084380"}, "osm_way"),
        ({"floors": [{"floor": "B5"}, {"floor": "B5"}]}, "floor twice"),
        ({"floors": [{"floor": "B5", "transform": {"origin_lat": 91, "origin_lon": 0, "scale": 1}}]}, "latitude"),
        ({"floors": [{"floor": "B5", "transform": {"origin_lat": 1, "origin_lon": 103, "scale": -1}}]}, "scale"),
        ({"floors": [{"floor": "B5", "plan": {"transform": {"origin_lat": 1, "origin_lon": 103, "scale": 1}}}]}, "floorplan_id"),
    ],
)
def test_bad_files_say_what_is_wrong(tmp_path: Path, change: dict, message: str):
    with pytest.raises(GeoError, match=message):
        load_campus_geo(_geo_file(tmp_path, **change))


def test_invalid_json_is_an_error(tmp_path: Path):
    path = tmp_path / "campus_geo.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(GeoError, match="not valid JSON"):
        load_campus_geo(path)


def test_committed_files_load():
    geo = load_campus_geo(
        PROJECT_ROOT / "data" / "campus_geo.json",
        PROJECT_ROOT / "data" / "campus_buildings.geojson",
    )
    assert {building.name for building in geo.buildings} >= {"Hive"}
    assert all(building.footprint for building in geo.buildings)


def test_stacked_hive_places_line_up(graph: CampusGraph):
    """Stairs and lifts on different floors are the same shaft, so the same spot.

    This is the check the committed transforms were fitted to pass; it fails
    if a floor's transform, or its places, are changed without the other.
    """
    geo = load_campus_geo(PROJECT_ROOT / "data" / "campus_geo.json")
    for name in ("Main Staircase", "Staircase 1", "Staircase 2", "Staircase 3"):
        shafts = [
            geo.position(node)
            for node in graph.nodes.values()
            if node.building == "Hive" and node.name == name
        ]
        assert len(shafts) == 3 and all(shafts)
        for other in shafts[1:]:
            assert _metres_between(shafts[0][:2], other[:2]) < 5


# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------


def _node(node_id: str, floor: str = "B5", x: float | None = 3, y: float | None = 4) -> Node:
    return Node(id=node_id, name=node_id, building="Hive", floor=floor, type="junction", x=x, y=y)


def test_place_is_positioned_through_its_floor(tmp_path: Path):
    geo = load_campus_geo(_geo_file(tmp_path))
    lon, lat, elevation = geo.position(_node("a"))
    assert _metres_between((lon, lat), (NTU[1], NTU[0])) == pytest.approx(5, abs=1e-6)
    assert elevation == 0


def test_unplaced_floor_or_missing_xy_gives_no_position(tmp_path: Path):
    geo = load_campus_geo(_geo_file(tmp_path))
    assert geo.position(_node("b", floor="B4")) is None
    assert geo.position(_node("c", x=None, y=None)) is None
    assert geo.position(_node("d", floor="L9")) is None


def test_pinned_place_wins_and_keeps_its_floor_height(tmp_path: Path):
    path = _geo_file(tmp_path)
    raw = json.loads(path.read_text())
    raw["places"] = {"b": {"lat": 1.3, "lon": 103.7}}
    _write(path, raw)

    geo = load_campus_geo(path)
    assert geo.position(_node("b", floor="B4")) == (103.7, 1.3, 4.5)


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(api.app) as test_client:
        yield test_client


def test_geo_endpoint_serves_the_campus(client: TestClient):
    response = client.get("/geo")
    assert response.status_code == 200
    body = response.json()

    hive = next(b for b in body["buildings"] if b["name"] == "Hive")
    assert hive["footprint"] and hive["height_m"] > 0
    assert {level["floor"] for level in hive["floors"]} == {"B3", "B4", "B5"}
    assert all(level["placed"] and level["plan"] for level in hive["floors"])

    lon, lat, elevation = body["places"]["Hive_B4_A"]
    assert 1.34 < lat < 1.35 and 103.68 < lon < 103.69
    assert elevation == 4.5
    # A place nobody has positioned is left out, not guessed at.
    assert "SS_B3_D" not in body["places"]


def test_geo_endpoint_is_empty_without_a_file(client: TestClient, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(client.app.state, "geo", CampusGeo(), raising=False)
    body = client.get("/geo").json()
    assert body["buildings"] == [] and body["places"] == {}


def test_scenery_leaves_out_the_named_buildings(client: TestClient):
    response = client.get("/geo/scenery")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/geo+json")
    features = response.json()["features"]

    named = {b["osm_way"] for b in client.get("/geo").json()["buildings"]}
    ways = {feature["properties"]["osm_way"] for feature in features}
    assert features and not (ways & named)
    assert all(feature["properties"]["height_m"] > 0 for feature in features)


def test_broken_geo_file_is_reported_not_hidden(monkeypatch, tmp_path: Path):
    broken = tmp_path / "campus_geo.json"
    broken.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(api, "CAMPUS_GEO_PATH", broken)

    with TestClient(api.app) as test_client:
        response = test_client.get("/geo")
        routes_still_work = test_client.get("/nodes")

    assert response.status_code == 503
    assert "JSON object" in response.json()["detail"]
    assert routes_still_work.status_code == 200

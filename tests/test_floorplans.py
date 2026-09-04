"""Tests for floorplan images and the coordinates that sit on them.

None of this data has been collected yet, so the behaviour that matters most
is what happens *without* it: an uncalibrated plan must say so rather than
guess where things are.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import app, get_floorplans_store
from shortcut.floorplan_store import (
    MAX_FLOORPLAN_BYTES,
    Floorplan,
    FloorplanStore,
    FloorplanStoreError,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend floorplan" * 8


@pytest.fixture
def floorplans(tmp_path: Path) -> FloorplanStore:
    return FloorplanStore(tmp_path / "floorplans")


@pytest.fixture
def client(tmp_path: Path, floorplans: FloorplanStore) -> Iterator[TestClient]:
    app.dependency_overrides[get_floorplans_store] = lambda: floorplans
    with TestClient(app) as test_client:
        app.state.floorplans = floorplans
        app.state.overrides_path = tmp_path / "graph_overrides.json"
        yield test_client
    app.dependency_overrides.clear()


def upload(client: TestClient, **fields):
    data = {"building": "Hive", "floor": "B5"}
    data.update(fields)
    return client.post(
        "/floorplans", files={"file": ("plan.png", PNG, "image/png")}, data=data
    )


# --------------------------------------------------------------------------
# The store on its own
# --------------------------------------------------------------------------


def test_a_new_store_is_empty(floorplans: FloorplanStore) -> None:
    assert floorplans.all() == []
    assert floorplans.for_floor("Hive", "B5") is None


def test_a_plan_can_be_uploaded_before_it_is_measured(
    floorplans: FloorplanStore,
) -> None:
    """Getting the image in is useful even before anyone has surveyed it."""
    plan = floorplans.add(
        content=PNG, content_type="image/png", building="Hive", floor="B5"
    )

    assert plan.is_calibrated is False
    assert floorplans.open_file(plan).read_bytes() == PNG


def test_an_uncalibrated_plan_refuses_to_place_a_point(
    floorplans: FloorplanStore,
) -> None:
    """A pin in the wrong place is worse than no pin."""
    plan = floorplans.add(
        content=PNG, content_type="image/png", building="Hive", floor="B5"
    )

    assert plan.to_pixels(10.0, 20.0) is None


def test_a_calibrated_plan_turns_metres_into_pixels(
    floorplans: FloorplanStore,
) -> None:
    plan = floorplans.add(
        content=PNG,
        content_type="image/png",
        building="Hive",
        floor="B5",
        origin_x_m=100.0,
        origin_y_m=200.0,
        metres_per_pixel=0.5,
    )

    # 10 m right of the origin, at half a metre per pixel, is 20 pixels across.
    assert plan.to_pixels(110.0, 210.0) == (20.0, 20.0)


def test_calibrating_later_works_the_same(floorplans: FloorplanStore) -> None:
    plan = floorplans.add(
        content=PNG, content_type="image/png", building="Hive", floor="B5"
    )

    updated = floorplans.calibrate(
        plan.id, origin_x_m=0.0, origin_y_m=0.0, metres_per_pixel=0.25
    )

    assert updated.is_calibrated is True
    assert updated.to_pixels(5.0, 0.0) == (20.0, 0.0)


def test_a_scale_of_zero_is_refused(floorplans: FloorplanStore) -> None:
    """Dividing by it would make every point land in the same place."""
    with pytest.raises(FloorplanStoreError):
        floorplans.add(
            content=PNG,
            content_type="image/png",
            building="Hive",
            floor="B5",
            metres_per_pixel=0.0,
        )


def test_a_file_that_is_not_an_image_is_refused(floorplans: FloorplanStore) -> None:
    with pytest.raises(FloorplanStoreError):
        floorplans.add(
            content=b"hello",
            content_type="text/plain",
            building="Hive",
            floor="B5",
        )


def test_a_file_that_is_too_large_is_refused(floorplans: FloorplanStore) -> None:
    with pytest.raises(FloorplanStoreError):
        floorplans.add(
            content=b"x" * (MAX_FLOORPLAN_BYTES + 1),
            content_type="image/png",
            building="Hive",
            floor="B5",
        )


def test_uploading_again_replaces_the_plan_for_that_floor(
    floorplans: FloorplanStore,
) -> None:
    """Replacing a plan should just be uploading a newer one."""
    floorplans.add(
        content=PNG, content_type="image/png", building="Hive", floor="B5"
    )
    newer = floorplans.add(
        content=PNG, content_type="image/png", building="Hive", floor="B5"
    )

    assert floorplans.for_floor("Hive", "B5").id == newer.id


def test_plans_are_kept_apart_by_floor(floorplans: FloorplanStore) -> None:
    b5 = floorplans.add(
        content=PNG, content_type="image/png", building="Hive", floor="B5"
    )
    b4 = floorplans.add(
        content=PNG, content_type="image/png", building="Hive", floor="B4"
    )

    assert floorplans.for_floor("Hive", "B5").id == b5.id
    assert floorplans.for_floor("Hive", "B4").id == b4.id


def test_deleting_a_plan_removes_its_file(floorplans: FloorplanStore) -> None:
    plan = floorplans.add(
        content=PNG, content_type="image/png", building="Hive", floor="B5"
    )
    path = floorplans.open_file(plan)

    assert floorplans.delete(plan.id) is True
    assert not path.exists()


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------


def test_a_plan_can_be_uploaded_and_fetched_back(client: TestClient) -> None:
    body = upload(client).json()

    assert body["is_calibrated"] is False
    fetched = client.get(body["url"])
    assert fetched.status_code == 200
    assert fetched.content == PNG


def test_a_plan_can_be_calibrated_through_the_api(client: TestClient) -> None:
    plan_id = upload(client).json()["id"]

    response = client.patch(
        f"/floorplans/{plan_id}",
        json={"origin_x_m": 0, "origin_y_m": 0, "metres_per_pixel": 0.05},
    )

    assert response.status_code == 200
    assert response.json()["is_calibrated"] is True


def test_a_scale_of_zero_is_refused_through_the_api(client: TestClient) -> None:
    plan_id = upload(client).json()["id"]

    response = client.patch(f"/floorplans/{plan_id}", json={"metres_per_pixel": 0})

    assert response.status_code == 422


def test_plans_can_be_looked_up_by_floor(client: TestClient) -> None:
    upload(client, floor="B5")
    upload(client, floor="B4")

    found = client.get(
        "/floorplans", params={"building": "Hive", "floor": "B4"}
    ).json()

    assert len(found) == 1
    assert found[0]["floor"] == "B4"


def test_asking_for_a_floor_with_no_plan_gives_an_empty_list(
    client: TestClient,
) -> None:
    """No plan yet is the normal state, not an error."""
    found = client.get(
        "/floorplans", params={"building": "Hive", "floor": "B5"}
    ).json()

    assert found == []


def test_a_plan_can_be_deleted(client: TestClient) -> None:
    plan_id = upload(client).json()["id"]

    assert client.delete(f"/floorplans/{plan_id}").status_code == 204
    assert client.get("/floorplans").json() == []


def test_fetching_a_plan_that_does_not_exist_is_a_404(client: TestClient) -> None:
    assert client.get("/floorplans/nosuchplan/file").status_code == 404


# --------------------------------------------------------------------------
# Node coordinates
# --------------------------------------------------------------------------


def test_places_start_without_coordinates(client: TestClient) -> None:
    """Nothing has been surveyed onto a plan yet, and the API says so."""
    nodes = client.get("/nodes").json()

    assert all(node["x"] is None and node["y"] is None for node in nodes)


def test_a_place_can_be_given_coordinates(client: TestClient) -> None:
    response = client.patch("/admin/nodes/Hive_B5_A", json={"x": 12.5, "y": 30.0})

    assert response.status_code == 200
    surveyed = next(n for n in client.get("/nodes").json() if n["id"] == "Hive_B5_A")
    assert (surveyed["x"], surveyed["y"]) == (12.5, 30.0)


def test_a_new_place_can_be_added_with_coordinates(client: TestClient) -> None:
    client.post(
        "/admin/nodes",
        json={
            "id": "Hive_B5_J",
            "name": "Study Pods",
            "building": "Hive",
            "floor": "B5",
            "x": 4.0,
            "y": 8.0,
        },
    )

    added = next(n for n in client.get("/nodes").json() if n["id"] == "Hive_B5_J")
    assert (added["x"], added["y"]) == (4.0, 8.0)

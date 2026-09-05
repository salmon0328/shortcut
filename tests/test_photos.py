"""Tests for storing photographs and showing them on a route.

A photo is always of something on the map and facing somewhere, which is what
lets a step show a picture looking the way the walker is going. These tests
care most about that pairing being respected.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import app, get_photos
from shortcut.photo_store import MAX_PHOTO_BYTES, PhotoStore, PhotoStoreError

# The store records photos, it never decodes them, so a header plus some bytes
# is a realistic enough upload.
PNG = b"\x89PNG\r\n\x1a\n" + b"pretend image data" * 4

CORRIDOR = "Hive_B5_002"  # Staircase 1 <-> Courtyard
COURTYARD = "Hive_B5_G"
STAIRCASE = "Hive_B5_A"


@pytest.fixture
def photos(tmp_path: Path) -> PhotoStore:
    return PhotoStore(tmp_path / "photos")


@pytest.fixture
def client(tmp_path: Path, photos: PhotoStore) -> Iterator[TestClient]:
    app.dependency_overrides[get_photos] = lambda: photos
    with TestClient(app) as test_client:
        app.state.photos = photos
        app.state.overrides_path = tmp_path / "graph_overrides.json"
        yield test_client
    app.dependency_overrides.clear()


def upload(client: TestClient, **fields):
    data = {"target_kind": "edge", "target_id": CORRIDOR}
    data.update(fields)
    return client.post(
        "/photos",
        files={"file": ("corridor.png", PNG, "image/png")},
        data=data,
    )


def route_steps(client: TestClient, origin: str, destination: str):
    response = client.post(
        "/route", json={"origin": origin, "destination": destination}
    )
    return response.json()["steps"]


# --------------------------------------------------------------------------
# The store on its own
# --------------------------------------------------------------------------


def test_a_new_store_is_empty(photos: PhotoStore) -> None:
    assert photos.all() == []


def test_a_photo_survives_a_round_trip(photos: PhotoStore) -> None:
    photo = photos.add(
        content=PNG,
        content_type="image/png",
        target_kind="edge",
        target_id=CORRIDOR,
        building="Hive",
        floor="B5",
        location="By the lockers",
        facing=COURTYARD,
    )

    assert photos.get(photo.id) == photo
    assert photos.read_file(photo) == PNG


def test_an_empty_file_is_refused(photos: PhotoStore) -> None:
    with pytest.raises(PhotoStoreError):
        photos.add(
            content=b"",
            content_type="image/png",
            target_kind="edge",
            target_id=CORRIDOR,
            building="Hive",
            floor="B5",
            location="",
        )


def test_a_file_that_is_too_large_is_refused(photos: PhotoStore) -> None:
    with pytest.raises(PhotoStoreError) as error:
        photos.add(
            content=b"x" * (MAX_PHOTO_BYTES + 1),
            content_type="image/png",
            target_kind="edge",
            target_id=CORRIDOR,
            building="Hive",
            floor="B5",
            location="",
        )

    assert "smaller than" in str(error.value)


def test_a_format_that_is_not_a_photo_is_refused(photos: PhotoStore) -> None:
    """These files are served back to browsers, so the type matters."""
    with pytest.raises(PhotoStoreError):
        photos.add(
            content=b"<script>",
            content_type="text/html",
            target_kind="edge",
            target_id=CORRIDOR,
            building="Hive",
            floor="B5",
            location="",
        )


def test_deleting_a_photo_removes_its_file(photos: PhotoStore) -> None:
    photo = photos.add(
        content=PNG,
        content_type="image/png",
        target_kind="edge",
        target_id=CORRIDOR,
        building="Hive",
        floor="B5",
        location="",
    )
    assert photos.delete(photo.id) is True
    assert photos.all() == []
    with pytest.raises(PhotoStoreError):
        photos.read_file(photo)


def test_the_right_photo_is_chosen_for_the_way_you_are_walking(
    photos: PhotoStore,
) -> None:
    towards_courtyard = photos.add(
        content=PNG,
        content_type="image/png",
        target_kind="edge",
        target_id=CORRIDOR,
        building="Hive",
        floor="B5",
        location="",
        facing=COURTYARD,
    )
    towards_staircase = photos.add(
        content=PNG,
        content_type="image/png",
        target_kind="edge",
        target_id=CORRIDOR,
        building="Hive",
        floor="B5",
        location="",
        facing=STAIRCASE,
    )

    assert photos.find_best("edge", CORRIDOR, COURTYARD) == towards_courtyard
    assert photos.find_best("edge", CORRIDOR, STAIRCASE) == towards_staircase


def test_no_photo_is_better_than_one_facing_the_wrong_way(
    photos: PhotoStore,
) -> None:
    """A picture looking back the way you came is worse than none at all."""
    photos.add(
        content=PNG,
        content_type="image/png",
        target_kind="edge",
        target_id=CORRIDOR,
        building="Hive",
        floor="B5",
        location="",
        facing=COURTYARD,
    )

    assert photos.find_best("edge", CORRIDOR, STAIRCASE) is None


def test_a_photo_with_no_direction_is_used_either_way(photos: PhotoStore) -> None:
    photos.add(
        content=PNG,
        content_type="image/png",
        target_kind="edge",
        target_id=CORRIDOR,
        building="Hive",
        floor="B5",
        location="",
        facing=None,
    )

    assert photos.find_best("edge", CORRIDOR, COURTYARD) is not None
    assert photos.find_best("edge", CORRIDOR, STAIRCASE) is not None


# --------------------------------------------------------------------------
# Uploading through the API
# --------------------------------------------------------------------------


def test_a_photo_can_be_uploaded(client: TestClient) -> None:
    response = upload(client, location="By the lockers", facing=COURTYARD)

    assert response.status_code == 201
    body = response.json()
    assert body["location"] == "By the lockers"
    assert body["facing"] == COURTYARD


def test_building_and_floor_default_to_the_place_they_are_of(
    client: TestClient,
) -> None:
    """Nobody should have to retype what the map already knows."""
    body = upload(client).json()

    assert body["building"] == "Hive"
    assert body["floor"] == "B5"


def test_building_and_floor_can_be_given_explicitly(client: TestClient) -> None:
    body = upload(client, building="Annexe", floor="L2").json()

    assert body["building"] == "Annexe"
    assert body["floor"] == "L2"


def test_the_uploaded_file_can_be_fetched_back(client: TestClient) -> None:
    url = upload(client).json()["url"]

    response = client.get(url)

    assert response.status_code == 200
    assert response.content == PNG


def test_a_photo_of_an_unknown_place_is_refused(client: TestClient) -> None:
    response = upload(client, target_id="Hive_B9_NOWHERE")

    assert response.status_code == 404


def test_facing_an_unknown_place_is_refused(client: TestClient) -> None:
    response = upload(client, facing="Hive_B9_NOWHERE")

    assert response.status_code == 404


def test_a_non_image_upload_is_refused(client: TestClient) -> None:
    response = client.post(
        "/photos",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"target_kind": "edge", "target_id": CORRIDOR},
    )

    assert response.status_code == 422


def test_photos_can_be_listed_for_one_place(client: TestClient) -> None:
    upload(client)
    upload(client, target_kind="node", target_id=COURTYARD)

    for_edge = client.get(
        "/photos", params={"target_kind": "edge", "target_id": CORRIDOR}
    ).json()

    assert len(for_edge) == 1
    assert for_edge[0]["target_id"] == CORRIDOR


def test_a_photo_can_be_deleted_through_the_api(client: TestClient) -> None:
    photo_id = upload(client).json()["id"]

    assert client.delete(f"/photos/{photo_id}").status_code == 204
    assert client.get("/photos").json() == []


def test_fetching_a_photo_that_does_not_exist_is_a_404(client: TestClient) -> None:
    assert client.get("/photos/nosuchphoto/file").status_code == 404


# --------------------------------------------------------------------------
# Photos on a route
# --------------------------------------------------------------------------


def test_a_step_carries_a_photo_facing_the_way_it_goes(client: TestClient) -> None:
    url = upload(client, facing=COURTYARD).json()["url"]

    steps = route_steps(client, STAIRCASE, COURTYARD)

    assert steps[0]["photo_url"] == url


def test_a_step_walked_the_other_way_does_not_reuse_that_photo(
    client: TestClient,
) -> None:
    upload(client, facing=COURTYARD)

    steps = route_steps(client, COURTYARD, STAIRCASE)

    assert steps[0]["photo_url"] is None


def test_steps_have_no_photo_when_none_has_been_taken(client: TestClient) -> None:
    steps = route_steps(client, STAIRCASE, COURTYARD)

    assert all(step["photo_url"] is None for step in steps)

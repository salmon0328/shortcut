"""Filing a photograph with a report, and keeping it out of the directions.

The rule this file exists to defend: **a picture of a problem is not a picture
of a place.** They are the same file format, taken on the same phone, of the
same corridor - and serving one as the other would show somebody a photograph
of a flooded stairwell captioned as the way they are walking, for as long as
it took anybody to notice.

Everything else here is ordinary: the upload works, the report carries the id,
the reviewer sees the evidence, and a report with no photo is still a report.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import app, get_photos, get_reports
from shortcut.photo_store import PhotoStore
from shortcut.report_store import ReportStore

REPORTED_EDGE = "Hive_B5_002"
REPORTED_NODE = "Hive_B5_G"

# The smallest thing a store will accept as a PNG. Content is never inspected,
# so a real photograph would prove nothing this does not.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


@pytest.fixture
def photos(tmp_path: Path) -> PhotoStore:
    return PhotoStore(tmp_path / "photos")


@pytest.fixture
def reports(tmp_path: Path) -> ReportStore:
    return ReportStore(tmp_path / "reports.json")


@pytest.fixture
def client(
    tmp_path: Path, photos: PhotoStore, reports: ReportStore
) -> Iterator[TestClient]:
    app.dependency_overrides[get_photos] = lambda: photos
    app.dependency_overrides[get_reports] = lambda: reports
    with TestClient(app) as test_client:
        app.state.photos = photos
        app.state.reports = reports
        app.state.overrides_path = tmp_path / "graph_overrides.json"
        yield test_client
    app.dependency_overrides.clear()


def upload(client: TestClient, target_kind="edge", target_id=REPORTED_EDGE,
           content=PNG, content_type="image/png"):
    return client.post(
        "/reports/photo",
        files={"file": ("problem.png", content, content_type)},
        data={"target_kind": target_kind, "target_id": target_id},
    )


def file_report(client: TestClient, photo_id=None, **overrides):
    payload = {
        "target_kind": "edge",
        "target_id": REPORTED_EDGE,
        "condition": "blocked",
        "notes": "barriers across it",
        "photo_id": photo_id,
    }
    payload.update(overrides)
    return client.post("/reports", json=payload)


# --------------------------------------------------------------------------
# Uploading
# --------------------------------------------------------------------------


def test_a_photo_of_a_problem_can_be_uploaded(client) -> None:
    response = upload(client)

    assert response.status_code == 201
    assert response.json()["id"]


def test_the_uploaded_file_can_be_fetched_back(client) -> None:
    photo_id = upload(client).json()["id"]

    fetched = client.get(f"/photos/{photo_id}/file")

    assert fetched.status_code == 200
    assert fetched.content == PNG


def test_it_takes_its_place_from_the_map(client, photos) -> None:
    """Nobody standing in a corridor should be asked which floor they are on."""
    photo_id = upload(client).json()["id"]

    stored = photos.get(photo_id)

    assert stored.building == "Hive"
    assert stored.floor == "B5"
    assert stored.location, "it records which corridor, in words"


def test_a_photo_of_nowhere_is_refused(client) -> None:
    response = upload(client, target_id="not-a-real-edge")

    assert response.status_code == 404


def test_something_that_is_not_an_image_is_refused(client) -> None:
    response = upload(client, content=b"not an image", content_type="text/plain")

    assert response.status_code == 422


# --------------------------------------------------------------------------
# The rule: evidence never becomes a direction
# --------------------------------------------------------------------------


def test_a_reported_photo_is_stored_as_evidence_not_as_a_place(client, photos) -> None:
    photo_id = upload(client).json()["id"]

    assert photos.get(photo_id).kind == "report"


def test_a_reported_photo_is_never_shown_as_a_direction(client, photos) -> None:
    """The one that matters.

    ``find_best`` is what the turn-by-turn screen calls for each step of a
    route. A photograph of the blockage answering that call would caption the
    problem as the way through it.
    """
    upload(client)

    assert photos.find_best("edge", REPORTED_EDGE, facing=None) is None
    assert photos.for_target("edge", REPORTED_EDGE) == []


def test_a_reported_photo_does_not_appear_on_a_route_step(client) -> None:
    """The same rule, checked where a walker would actually meet it."""
    upload(client)

    route = client.post(
        "/route", json={"origin": "Hive_B5_A", "destination": "Hive_B5_I"}
    ).json()

    photographed = [s for s in route["steps"] if s.get("photo")]
    assert photographed == [], "an evidence photo reached the directions"


def test_a_place_photo_still_reaches_the_directions(client, photos) -> None:
    """The guard above must not have turned every photo off.

    Uploaded through the ordinary photo endpoint, which is the one that means
    "this is what this corridor looks like".
    """
    client.post(
        "/photos",
        files={"file": ("corridor.png", PNG, "image/png")},
        data={
            "target_kind": "edge",
            "target_id": REPORTED_EDGE,
            "facing": "Hive_B5_C",
        },
    )

    assert photos.find_best("edge", REPORTED_EDGE, facing="Hive_B5_C") is not None


def test_the_two_kinds_coexist_without_confusing_each_other(client, photos) -> None:
    upload(client)
    client.post(
        "/photos",
        files={"file": ("corridor.png", PNG, "image/png")},
        data={"target_kind": "edge", "target_id": REPORTED_EDGE},
    )

    assert len(photos.for_target("edge", REPORTED_EDGE, kind=None)) == 2
    assert len(photos.for_target("edge", REPORTED_EDGE)) == 1
    assert len(photos.for_target("edge", REPORTED_EDGE, kind="report")) == 1


# --------------------------------------------------------------------------
# Filing it with the report
# --------------------------------------------------------------------------


def test_a_report_carries_the_photo_it_was_filed_with(client) -> None:
    photo_id = upload(client).json()["id"]

    report = file_report(client, photo_id).json()

    assert report["photo_id"] == photo_id


def test_a_report_without_a_photo_is_still_a_report(client) -> None:
    """Optional means optional.

    Somebody standing in a blocked corridor with somewhere to be should be
    able to file in three taps.
    """
    response = file_report(client)

    assert response.status_code == 201
    assert response.json()["photo_id"] is None


def test_a_photo_id_that_names_nothing_is_refused(client) -> None:
    """Rather than stored, which would leave a broken image in the queue and
    read as a bug in the queue rather than a bad submission."""
    response = file_report(client, "not-a-real-photo")

    assert response.status_code == 404


def test_the_review_queue_carries_the_evidence(client) -> None:
    photo_id = upload(client).json()["id"]
    file_report(client, photo_id)
    file_report(client)  # a second voice, with no photo

    group = client.get("/reports/groups").json()[0]

    assert group["confirmations"] == 2
    assert group["photo_ids"] == [photo_id], "only the ones actually filed"


def test_photos_arrive_in_the_order_they_were_reported(client) -> None:
    first = upload(client).json()["id"]
    file_report(client, first)
    second = upload(client).json()["id"]
    file_report(client, second)

    group = client.get("/reports/groups").json()[0]

    assert group["photo_ids"] == [first, second]


def test_a_photo_survives_the_report_being_approved(client) -> None:
    """An approved report is the record of why the map changed, and the
    picture is the best part of that record."""
    photo_id = upload(client).json()["id"]
    file_report(client, photo_id)
    key = f"blocked:{REPORTED_EDGE}"

    client.post(f"/reports/groups/{key}/approve")

    settled = client.get("/reports?report_status=approved").json()
    assert settled[0]["photo_id"] == photo_id
    assert client.get(f"/photos/{photo_id}/file").status_code == 200

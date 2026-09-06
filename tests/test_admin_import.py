"""Uploading a drawing, reviewing what it says, and deciding on it.

The rule this whole feature exists to keep: **nothing a machine read out of a
PDF routes anybody anywhere until a person has looked at it.** So the tests
that matter most are the ones about what an unapproved candidate cannot do.

Every test writes to a temporary overrides file and a temporary queue, so the
surveyed map and this machine's own state are never touched.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import FLOORPLANS_DIR, app, get_candidates, get_photos
from shortcut.candidate_store import CandidateStore
from shortcut.floorplan_store import FloorplanStore
from shortcut.import_source_store import ImportSourceStore
from shortcut.photo_store import PhotoStore

NODE_MAP = (
    Path(__file__).resolve().parents[1] / "data" / "survey_sources" / "hive_node_map.pdf"
)


@pytest.fixture(scope="module")
def drawing_bytes() -> bytes:
    return NODE_MAP.read_bytes()


@pytest.fixture
def queue(tmp_path: Path) -> CandidateStore:
    return CandidateStore(tmp_path / "import_candidates.json")


@pytest.fixture
def client(tmp_path: Path, queue: CandidateStore) -> Iterator[TestClient]:
    # A copy of the real plans, not the real plans. Approving a floorplan
    # writes one, and a test that wrote into ``data/floorplans`` would leave a
    # machine's own map quietly different from everybody else's. Copied rather
    # than left empty because what the surveyed floors already have is what
    # decides whether an uploaded place arrives with a position.
    plans = tmp_path / "floorplans"
    shutil.copytree(FLOORPLANS_DIR, plans)

    photos = PhotoStore(tmp_path / "photos")
    app.dependency_overrides[get_photos] = lambda: photos
    app.dependency_overrides[get_candidates] = lambda: queue
    with TestClient(app) as test_client:
        app.state.photos = photos
        app.state.candidates = queue
        app.state.floorplans = FloorplanStore(plans)
        app.state.import_sources = ImportSourceStore(tmp_path / "imports")
        app.state.overrides_path = tmp_path / "graph_overrides.json"
        yield test_client
    app.dependency_overrides.clear()


def upload(client: TestClient, content: bytes, name: str = "map.pdf"):
    return client.post(
        "/admin/import", files={"files": (name, content, "application/pdf")}
    )


def candidates(client: TestClient) -> list[dict]:
    return client.get("/admin/import/candidates").json()


def first_of(client: TestClient, kind: str) -> dict:
    return next(c for c in candidates(client) if c["kind"] == kind)


# --------------------------------------------------------------------------
# Uploading
# --------------------------------------------------------------------------


def test_a_drawing_is_read_into_things_to_review(
    client: TestClient, drawing_bytes: bytes
) -> None:
    response = upload(client, drawing_bytes)

    assert response.status_code == 201
    body = response.json()
    assert body["added"] > 0
    assert body["candidates"]


def test_what_the_map_already_has_is_counted_not_offered(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Recognised rather than re-offered, and said out loud either way.

    Stated as a property, not as a count. An earlier version asserted that
    most of this drawing was already known - true while the survey held every
    floor the drawing does, and false the moment somebody legitimately
    shrinks it, which is a test breaking on a change it should not care about.
    """
    body = upload(client, drawing_bytes).json()
    on_the_map = {node["id"] for node in client.get("/nodes").json()}

    assert body["already_known"] > 0, "this drawing does contain surveyed floors"
    assert not any(c["target_id"] in on_the_map for c in body["candidates"])


def test_lines_the_drawing_did_not_settle_are_reported(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Times written '?s', connectors drawn off the edge of the page."""
    body = upload(client, drawing_bytes).json()

    assert body["unsettled"]


def test_a_file_that_is_not_a_drawing_is_refused(client: TestClient) -> None:
    response = upload(client, b"not a pdf at all", name="notes.txt")

    assert response.status_code == 422
    assert "could not be read" in response.json()["detail"]


def test_uploading_the_same_drawing_twice_does_not_double_the_queue(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """A reviewer gets halfway, comes back, and uploads again. That is normal."""
    first = upload(client, drawing_bytes).json()
    second = upload(client, drawing_bytes).json()

    assert second["added"] == 0
    assert second["already_waiting"] == first["added"]
    assert len(candidates(client)) == first["added"]


# --------------------------------------------------------------------------
# What a candidate cannot do
# --------------------------------------------------------------------------


def test_an_unapproved_place_is_not_on_the_map(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """The whole point. A machine read it; nobody has checked it."""
    upload(client, drawing_bytes)
    waiting = {c["target_id"] for c in candidates(client) if c["kind"] == "node"}

    on_the_map = {node["id"] for node in client.get("/nodes").json()}

    assert waiting, "sanity: this drawing does offer new places"
    assert not (waiting & on_the_map)


def test_an_unapproved_place_cannot_be_routed_to(
    client: TestClient, drawing_bytes: bytes
) -> None:
    upload(client, drawing_bytes)
    waiting = first_of(client, "node")["target_id"]

    response = client.post(
        "/route", json={"origin": "Hive_B5_A", "destination": waiting}
    )

    assert response.status_code == 404


def test_an_unapproved_candidate_is_not_a_pending_change(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """/admin/pending lists the overrides file, and nothing has gone there."""
    upload(client, drawing_bytes)

    assert client.get("/admin/pending").json()["total"] == 0


def test_the_surveyed_file_is_never_written(
    client: TestClient, drawing_bytes: bytes, graph_path: Path
) -> None:
    before = graph_path.read_text(encoding="utf-8")
    upload(client, drawing_bytes)
    client.post("/admin/import/approve-all")

    assert graph_path.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# Correcting one before approving it
# --------------------------------------------------------------------------


def test_a_stand_in_name_can_be_replaced(
    client: TestClient, drawing_bytes: bytes
) -> None:
    upload(client, drawing_bytes)
    candidate = first_of(client, "node")
    assert candidate["name_is_a_stand_in"] is True

    body = client.patch(
        f"/admin/import/candidates/{candidate['id']}", json={"name": "Study Pods"}
    ).json()

    assert body["fields"]["name"] == "Study Pods"
    assert body["name_is_a_stand_in"] is False


def test_a_correction_survives_into_the_map(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Editing then approving must add what the reviewer said, not what was read."""
    upload(client, drawing_bytes)
    candidate = first_of(client, "node")
    client.patch(
        f"/admin/import/candidates/{candidate['id']}",
        json={"name": "Study Pods", "type": "room"},
    )

    client.post(f"/admin/import/candidates/{candidate['id']}/approve")

    added = next(
        node
        for node in client.get("/nodes").json()
        if node["id"] == candidate["target_id"]
    )
    assert added["name"] == "Study Pods"


def test_a_field_the_wrong_kind_has_no_place_for_is_refused(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Dropping it silently would look like the edit worked."""
    upload(client, drawing_bytes)
    node = first_of(client, "node")

    response = client.patch(
        f"/admin/import/candidates/{node['id']}", json={"walk_seconds": 12}
    )

    assert response.status_code == 422
    assert "walk_seconds" in response.json()["detail"]


def test_correcting_something_that_is_not_waiting_is_a_404(
    client: TestClient,
) -> None:
    response = client.patch("/admin/import/candidates/nosuch", json={"name": "x"})

    assert response.status_code == 404


# --------------------------------------------------------------------------
# Approving
# --------------------------------------------------------------------------


def test_approving_puts_a_place_on_the_map(
    client: TestClient, drawing_bytes: bytes
) -> None:
    upload(client, drawing_bytes)
    candidate = first_of(client, "node")

    response = client.post(f"/admin/import/candidates/{candidate['id']}/approve")

    assert response.status_code == 200
    assert response.json()["approved"] is True
    assert candidate["target_id"] in {n["id"] for n in client.get("/nodes").json()}


def test_an_approved_place_leaves_the_queue(
    client: TestClient, drawing_bytes: bytes
) -> None:
    upload(client, drawing_bytes)
    candidate = first_of(client, "node")

    client.post(f"/admin/import/candidates/{candidate['id']}/approve")

    assert candidate["id"] not in {c["id"] for c in candidates(client)}


def test_an_approved_place_becomes_a_pending_change(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Approving means "into the overrides file", which is what /admin/pending
    shows - so it now sits beside everything added by hand, awaiting the same
    manual graduation into the survey."""
    upload(client, drawing_bytes)
    candidate = first_of(client, "node")

    client.post(f"/admin/import/candidates/{candidate['id']}/approve")

    pending = client.get("/admin/pending").json()
    assert candidate["target_id"] in {change["id"] for change in pending["changes"]}


def test_a_link_cannot_be_approved_before_the_places_it_joins(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Approving it would leave an edge pointing at a place that does not exist."""
    upload(client, drawing_bytes)
    blocked = next(
        (c for c in candidates(client) if c["kind"] == "edge" and c["blocked_by"]),
        None,
    )
    assert blocked is not None, "sanity: this drawing has links to new places"

    response = client.post(f"/admin/import/candidates/{blocked['id']}/approve")

    assert response.status_code == 409
    assert "Approve the place before the link" in response.json()["detail"]


def test_the_screen_is_told_what_has_to_be_approved_first(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """So a button can be greyed out with a reason, rather than just failing."""
    upload(client, drawing_bytes)
    blocked = next(c for c in candidates(client) if c["blocked_by"])

    waiting = {c["id"]: c for c in candidates(client)}
    for blocker_id in blocked["blocked_by"]:
        assert waiting[blocker_id]["kind"] == "node"


def test_approving_everything_does_places_before_links(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Otherwise the button is useless on any real drawing."""
    upload(client, drawing_bytes)
    before = len(candidates(client))

    response = client.post("/admin/import/approve-all")

    assert response.status_code == 200
    body = response.json()
    assert len(body["approved"]) == before
    assert body["skipped"] == []
    assert candidates(client) == []


def test_one_candidate_that_cannot_be_applied_does_not_sink_the_batch(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Rejecting a place and then approving the rest is an ordinary thing to do.

    It used to approve everything up to the orphaned link, then let that
    link's refusal propagate - so thirty-six places went live and the caller
    got a bare 409 implying nothing had happened.
    """
    upload(client, drawing_bytes)
    blocked = next(c for c in candidates(client) if c["kind"] == "edge" and c["blocked_by"])
    client.delete(f"/admin/import/candidates/{blocked['blocked_by'][0]}")

    response = client.post("/admin/import/approve-all")

    assert response.status_code == 200
    body = response.json()
    assert body["approved"], "the ones that could be applied still were"
    assert any(s["id"] == blocked["id"] for s in body["skipped"])


def test_a_skipped_candidate_says_why_and_stays_in_the_queue(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Nobody decided anything about it, so it is still waiting for somebody."""
    upload(client, drawing_bytes)
    blocked = next(c for c in candidates(client) if c["kind"] == "edge" and c["blocked_by"])
    client.delete(f"/admin/import/candidates/{blocked['blocked_by'][0]}")

    body = client.post("/admin/import/approve-all").json()

    skipped = next(s for s in body["skipped"] if s["id"] == blocked["id"])
    assert "not on the map yet" in skipped["reason"]
    assert blocked["id"] in {c["id"] for c in candidates(client)}
    assert body["remaining"] == len(candidates(client))


def test_everything_reported_as_approved_really_is_there(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """The report has to match reality, especially when part of it failed.

    Where "there" lands depends on the kind: a place or a link joins the map,
    a floorplan joins the floorplan store, since nothing is routed over a
    picture.
    """
    upload(client, drawing_bytes)
    kinds = {c["id"]: c["kind"] for c in candidates(client)}
    blocked = next(c for c in candidates(client) if c["kind"] == "edge" and c["blocked_by"])
    client.delete(f"/admin/import/candidates/{blocked['blocked_by'][0]}")

    body = client.post("/admin/import/approve-all").json()

    on_the_map = {node["id"] for node in client.get("/nodes").json()} | {
        edge["id"] for edge in client.get("/edges").json()
    }
    stored_plans = {
        (plan["building"], plan["floor"]) for plan in client.get("/floorplans").json()
    }

    for result in body["approved"]:
        if kinds[result["id"]] == "floorplan":
            building, floor = result["target_id"].split("|")
            assert (building, floor) in stored_plans
        else:
            assert result["target_id"] in on_the_map


def test_approving_everything_leaves_a_map_that_still_loads(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """The strongest thing to check: no dangling edge, no unroutable graph."""
    upload(client, drawing_bytes)

    client.post("/admin/import/approve-all")

    assert client.get("/nodes").status_code == 200
    assert (
        client.post(
            "/route", json={"origin": "Hive_B5_A", "destination": "Hive_B5_I"}
        ).status_code
        == 200
    )


def test_approving_something_that_is_not_waiting_is_a_404(client: TestClient) -> None:
    response = client.post("/admin/import/candidates/nosuch/approve")

    assert response.status_code == 404


# --------------------------------------------------------------------------
# Floorplans
# --------------------------------------------------------------------------


def plans_offered(client: TestClient) -> list[dict]:
    return [c for c in candidates(client) if c["kind"] == "floorplan"]


def test_a_drawing_offers_a_plan_for_the_floors_it_covers(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """The picture a floor is drawn on is as much a reading of the drawing as
    the places on it, and it used to be the one thing only a script could
    upload."""
    upload(client, drawing_bytes)

    assert plans_offered(client), "this drawing does contain floorplans"


def test_a_floorplan_is_not_stored_until_it_is_approved(
    client: TestClient, drawing_bytes: bytes
) -> None:
    before = client.get("/floorplans").json()

    upload(client, drawing_bytes)

    assert client.get("/floorplans").json() == before


def test_approving_a_floorplan_stores_it_calibrated(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """A plan nobody has calibrated is a picture: the app knows the image
    exists and still cannot say where on it a place is."""
    upload(client, drawing_bytes)
    plan = plans_offered(client)[0]
    building, floor = plan["target_id"].split("|")

    assert client.post(f"/admin/import/candidates/{plan['id']}/approve").status_code == 200

    stored = next(
        p
        for p in client.get("/floorplans").json()
        if (p["building"], p["floor"]) == (building, floor)
    )
    assert stored["is_calibrated"] is True
    assert stored["metres_per_pixel"] > 0


def test_a_floorplan_says_how_far_its_links_disagreed(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """A floor whose links disagree is a floor traced badly, and the number
    saying so is worth more than the scale it produced."""
    upload(client, drawing_bytes)

    for plan in plans_offered(client):
        assert plan["fields"]["links_used"] >= 3
        assert "spread" in plan["fields"]
        assert isinstance(plan["fields"]["well_conditioned"], bool)


def test_a_floor_drawn_across_several_crops_is_offered_as_one_plan(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """SS B3 is three images on one page. One plan, or the places on two of
    them have nowhere to be drawn."""
    upload(client, drawing_bytes)

    composed = [p for p in plans_offered(client) if p["fields"]["made_of"] > 1]
    assert composed, "this drawing draws at least one floor across several crops"


def test_approving_a_plan_places_the_places_still_waiting_on_it(
    client: TestClient, queue: CandidateStore, drawing_bytes: bytes
) -> None:
    """The loop this closes: a place queued before its floor had a plan has
    nothing to be measured against, so it waits with no position. Approving
    the plan is the moment that answer exists - and without this the reviewer
    would approve a floor of places that never appear on the map.

    The waiting is arranged here rather than found in the drawing. This one
    upload measures its own floors, so every place it queues on a floor with a
    plan arrives placed already; a place waiting is what happens when the plan
    turns up in a *later* upload than the place did, which is a two-drawing
    story that this drawing cannot tell on its own.
    """
    upload(client, drawing_bytes)

    plan = next(p for p in plans_offered(client) if p["target_id"] == "Hive|B4")
    # A place the drawing does name, queued as an upload with no plan for its
    # floor would have queued it: named, on the right floor, and nowhere.
    waiting = queue.add_many(
        [
            {
                "kind": "node",
                "target_id": "Hive_B4_A",
                "fields": {
                    "name": "Hive B4 A",
                    "building": "Hive",
                    "floor": "B4",
                    "x": None,
                    "y": None,
                },
                "source": "an earlier drawing",
            }
        ]
    )[0]

    client.post(f"/admin/import/candidates/{plan['id']}/approve")

    placed = next(c for c in candidates(client) if c["id"] == waiting.id)
    assert placed["fields"]["x"] is not None
    assert placed["fields"]["y"] is not None


def test_approving_everything_does_plans_before_the_places_on_them(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Otherwise a whole floor of places lands with no position at all.

    Checked against the floors the drawing could actually scale: a floor it
    refused to fit a scale for has no plan to place anything against, and its
    places are meant to arrive unplaced.
    """
    upload(client, drawing_bytes)
    scalable = {p["target_id"] for p in plans_offered(client)}
    added = {
        c["target_id"]: c["fields"]
        for c in candidates(client)
        if c["kind"] == "node"
        and f"{c['fields'].get('building')}|{c['fields'].get('floor')}" in scalable
    }
    assert added, "sanity: some new places are on floors that do have a plan"

    client.post("/admin/import/approve-all")

    on_the_map = {node["id"]: node for node in client.get("/nodes").json()}
    for node_id in added:
        assert on_the_map[node_id]["x"] is not None, f"{node_id} landed with no position"


def test_rejecting_a_floorplan_stores_nothing(
    client: TestClient, drawing_bytes: bytes
) -> None:
    before = client.get("/floorplans").json()
    upload(client, drawing_bytes)
    plan = plans_offered(client)[0]

    client.delete(f"/admin/import/candidates/{plan['id']}")

    assert client.get("/floorplans").json() == before
    assert plan["id"] not in {c["id"] for c in candidates(client)}


# --------------------------------------------------------------------------
# Rejecting
# --------------------------------------------------------------------------


def test_rejecting_throws_it_away(client: TestClient, drawing_bytes: bytes) -> None:
    upload(client, drawing_bytes)
    candidate = first_of(client, "node")

    response = client.delete(f"/admin/import/candidates/{candidate['id']}")

    assert response.status_code == 200
    assert response.json()["approved"] is False
    assert candidate["id"] not in {c["id"] for c in candidates(client)}


def test_rejecting_changes_nothing_about_the_map(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """It was never live, so there is nothing to undo."""
    before = client.get("/nodes").json()
    upload(client, drawing_bytes)
    candidate = first_of(client, "node")

    client.delete(f"/admin/import/candidates/{candidate['id']}")

    assert client.get("/nodes").json() == before
    assert client.get("/admin/pending").json()["total"] == 0


def test_rejecting_something_that_is_not_waiting_is_a_404(
    client: TestClient,
) -> None:
    assert client.delete("/admin/import/candidates/nosuch").status_code == 404


# --------------------------------------------------------------------------
# Telling a reviewer what they are looking at
# --------------------------------------------------------------------------


def test_a_link_between_two_surveyed_places_is_flagged_as_a_disagreement(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """Either the drawing is newer than the survey, or it is older and its
    lettering means different places by the same names. Shown as a plain
    addition, a reviewer cannot tell which."""
    upload(client, drawing_bytes)

    flagged = [c for c in candidates(client) if c["disagrees_with_survey"]]

    assert flagged, "this drawing does contradict the survey"


def test_every_candidate_says_where_it_came_from(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """So a reviewer can go and look at the page it was read off."""
    upload(client, drawing_bytes, name="hive_node_map.pdf")

    for candidate in candidates(client):
        assert "hive_node_map.pdf" in candidate["source"]
        assert "page" in candidate["source"]


def test_what_the_drawing_wrote_on_a_link_is_shown(
    client: TestClient, drawing_bytes: bytes
) -> None:
    """The reviewer is told why stairs was ticked, not asked to trust it."""
    upload(client, drawing_bytes)

    for candidate in candidates(client):
        if candidate["kind"] == "edge" and candidate["fields"]["stairs"]:
            assert candidate["marks"], "a ticked stairs flag needs its evidence"

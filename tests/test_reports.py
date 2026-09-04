"""Tests for reporting problems, and for reviewing those reports.

Two layers are covered: the store on its own, and the endpoints on top of it.
Every test writes to a temporary reports file, so the real
``data/reports.json`` is never touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import app, get_reports
from shortcut.report_store import ReportStore

# A corridor on the quickest Hive_B5_A -> Hive_B4_C route, so blocking it has
# a visible effect on routing.
REPORTED_EDGE = "Hive_B5_002"
BLOCKED_KEY = f"blocked:{REPORTED_EDGE}"
CROWDED_NODE = "Hive_B5_G"
CROWDED_KEY = f"crowded:{CROWDED_NODE}"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> ReportStore:
    """A report store backed by a throwaway file."""
    return ReportStore(tmp_path / "reports.json")


@pytest.fixture
def client(tmp_path: Path, store: ReportStore) -> Iterator[TestClient]:
    """A client whose reports and map overrides both live in tmp_path.

    The overrides path is set after startup, because the app's own lifespan
    points it at the real ``data/`` folder. Approving a report writes there
    and reloads the graph, so it has to be redirected before any request runs.
    """
    app.dependency_overrides[get_reports] = lambda: store
    with TestClient(app) as test_client:
        app.state.reports = store
        app.state.overrides_path = tmp_path / "graph_overrides.json"
        yield test_client
    app.dependency_overrides.clear()


def submit(client: TestClient, **overrides):
    payload = {
        "target_kind": "edge",
        "target_id": REPORTED_EDGE,
        "condition": "blocked",
        "notes": "",
    }
    payload.update(overrides)
    return client.post("/reports", json=payload)


def route_edges(client: TestClient) -> list[str]:
    response = client.post(
        "/route", json={"origin": "Hive_B5_A", "destination": "Hive_B4_C"}
    )
    return response.json()["edges"]


# --------------------------------------------------------------------------
# The store on its own
# --------------------------------------------------------------------------


def test_a_missing_reports_file_reads_as_empty(store: ReportStore) -> None:
    """Nobody has reported anything yet, which is not an error."""
    assert store.all() == []
    assert store.pending_groups() == []


def test_every_submission_is_kept_separately(store: ReportStore) -> None:
    """Two people reporting one problem must leave two rows, not one.

    The count shown to an administrator is worked out at read time, so the
    individual submissions stay available for checking.
    """
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")

    assert len(store.all()) == 2
    assert len({report.id for report in store.all()}) == 2


def test_matching_reports_are_grouped_with_a_count(store: ReportStore) -> None:
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")

    groups = store.pending_groups()

    assert len(groups) == 1
    assert groups[0].confirmations == 2
    assert len(groups[0].report_ids) == 2


def test_a_different_condition_is_a_different_problem(store: ReportStore) -> None:
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="flooded")

    assert len(store.pending_groups()) == 2


def test_a_different_place_is_a_different_problem(store: ReportStore) -> None:
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")
    store.add(target_kind="edge", target_id="Hive_B5_007", condition="blocked")

    assert len(store.pending_groups()) == 2


def test_the_queue_leads_with_the_best_corroborated_problem(
    store: ReportStore,
) -> None:
    store.add(target_kind="edge", target_id="Hive_B5_007", condition="blocked")
    for _ in range(3):
        store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")

    groups = store.pending_groups()

    assert groups[0].target_id == REPORTED_EDGE
    assert groups[0].confirmations == 3


def test_notes_are_collected_for_review(store: ReportStore) -> None:
    store.add(
        target_kind="edge", target_id=REPORTED_EDGE, condition="blocked", notes="Barriers"
    )
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked", notes="")

    group = store.pending_groups()[0]

    assert group.notes == ("Barriers",)  # blank notes are not worth showing


def test_reviewing_a_group_updates_all_of_its_reports(store: ReportStore) -> None:
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")

    changed = store.set_group_status(BLOCKED_KEY, "approved")

    assert len(changed) == 2
    assert {report.status for report in store.all()} == {"approved"}
    assert all(report.reviewed_at for report in store.all())


def test_a_reviewed_group_leaves_the_pending_queue(store: ReportStore) -> None:
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")

    store.set_group_status(BLOCKED_KEY, "rejected")

    assert store.pending_groups() == []
    assert len(store.all()) == 1, "the report itself must still be on record"


def test_reviewing_an_unknown_group_changes_nothing(store: ReportStore) -> None:
    store.add(target_kind="edge", target_id=REPORTED_EDGE, condition="blocked")

    assert store.set_group_status("blocked:nowhere", "approved") == []
    assert store.all()[0].status == "pending"


# --------------------------------------------------------------------------
# Submitting through the API
# --------------------------------------------------------------------------


def test_a_report_is_accepted_and_starts_pending(client: TestClient) -> None:
    response = submit(client, notes="Barriers across the corridor.")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["notes"] == "Barriers across the corridor."
    assert body["id"]


def test_submitting_a_report_changes_nothing_on_its_own(client: TestClient) -> None:
    """One person must not be able to reroute everybody."""
    before = route_edges(client)

    submit(client)

    assert route_edges(client) == before


def test_a_report_about_an_unknown_place_is_refused(client: TestClient) -> None:
    """Reports name places from the map, which is what makes them groupable."""
    response = submit(client, target_id="Hive_B9_NOWHERE")

    assert response.status_code == 404


def test_an_unknown_condition_is_refused(client: TestClient) -> None:
    response = submit(client, condition="haunted")

    assert response.status_code == 422


def test_a_node_can_be_reported_too(client: TestClient) -> None:
    response = submit(client, target_kind="node", target_id=CROWDED_NODE, condition="crowded")

    assert response.status_code == 201


# --------------------------------------------------------------------------
# Listing reports
# --------------------------------------------------------------------------


def test_every_report_is_listed_individually(client: TestClient) -> None:
    """The raw list is what makes it possible to check what people sent."""
    submit(client, notes="first")
    submit(client, notes="second")

    body = client.get("/reports").json()

    assert len(body) == 2
    assert [report["notes"] for report in body] == ["first", "second"]


def test_reports_can_be_filtered_by_status(client: TestClient) -> None:
    submit(client)
    submit(client, target_id="Hive_B5_007")
    client.post(f"/reports/groups/{BLOCKED_KEY}/reject")

    pending = client.get("/reports", params={"report_status": "pending"}).json()
    rejected = client.get("/reports", params={"report_status": "rejected"}).json()

    assert [r["target_id"] for r in pending] == ["Hive_B5_007"]
    assert [r["target_id"] for r in rejected] == [REPORTED_EDGE]


def test_the_review_queue_names_the_place_readably(client: TestClient) -> None:
    """An admin should not have to decode 'Hive_B5_002' by hand."""
    submit(client)

    group = client.get("/reports/groups").json()[0]

    assert group["target_name"] == "Staircase 1 → Courtyard"
    assert group["confirmations"] == 1


def test_the_queue_says_whether_approving_would_block_a_route(
    client: TestClient,
) -> None:
    submit(client)
    submit(client, target_kind="node", target_id=CROWDED_NODE, condition="crowded")

    groups = {group["key"]: group for group in client.get("/reports/groups").json()}

    assert groups[BLOCKED_KEY]["blocks_routes"] is True
    assert groups[CROWDED_KEY]["blocks_routes"] is False


# --------------------------------------------------------------------------
# Approving and rejecting
# --------------------------------------------------------------------------


def test_approving_a_blockage_reroutes_around_it(client: TestClient) -> None:
    before = route_edges(client)
    assert REPORTED_EDGE in before, "sanity: the corridor is on the quickest route"
    submit(client)

    result = client.post(f"/reports/groups/{BLOCKED_KEY}/approve").json()

    assert result["routing_changed"] is True
    assert REPORTED_EDGE not in route_edges(client)


def test_approving_a_crowded_report_warns_without_rerouting(
    client: TestClient,
) -> None:
    """A busy corridor is still walkable, so routing must not avoid it."""
    before = route_edges(client)
    submit(client, target_kind="node", target_id=CROWDED_NODE, condition="crowded")

    result = client.post(f"/reports/groups/{CROWDED_KEY}/approve").json()

    assert result["routing_changed"] is False
    assert route_edges(client) == before

    node = next(n for n in client.get("/nodes").json() if n["id"] == CROWDED_NODE)
    assert node["condition"] == "crowded"


def test_approving_marks_every_report_in_the_group(client: TestClient) -> None:
    submit(client)
    submit(client)

    result = client.post(f"/reports/groups/{BLOCKED_KEY}/approve").json()

    assert result["reports_updated"] == 2
    assert {r["status"] for r in client.get("/reports").json()} == {"approved"}


def test_rejecting_leaves_the_map_alone(client: TestClient) -> None:
    before = route_edges(client)
    submit(client)

    result = client.post(f"/reports/groups/{BLOCKED_KEY}/reject").json()

    assert result["routing_changed"] is False
    assert route_edges(client) == before
    assert client.get("/reports").json()[0]["status"] == "rejected"


def test_a_reviewed_group_disappears_from_the_queue(client: TestClient) -> None:
    submit(client)

    client.post(f"/reports/groups/{BLOCKED_KEY}/approve")

    assert client.get("/reports/groups").json() == []


def test_reviewing_the_same_group_twice_is_refused(client: TestClient) -> None:
    submit(client)
    client.post(f"/reports/groups/{BLOCKED_KEY}/approve")

    response = client.post(f"/reports/groups/{BLOCKED_KEY}/approve")

    assert response.status_code == 404


def test_reviewing_a_group_nobody_reported_is_refused(client: TestClient) -> None:
    response = client.post("/reports/groups/blocked:Hive_B5_007/approve")

    assert response.status_code == 404


def test_the_surveyed_graph_file_is_never_rewritten(
    client: TestClient, graph_path: Path
) -> None:
    """Approving changes routing through the overrides file, not the survey."""
    before = graph_path.read_text(encoding="utf-8")
    submit(client)

    client.post(f"/reports/groups/{BLOCKED_KEY}/approve")

    assert graph_path.read_text(encoding="utf-8") == before

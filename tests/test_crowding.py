"""Crowding: reported by people, priced by the router, forgotten on its own.

The behaviour these pin, in one sentence: a busy place gets slower and then
gets better by itself, and it never becomes unwalkable however many people
complain.

Everything here builds its own reports rather than going through the API, so
the arithmetic is visible in the test. The last section checks it survives the
trip through HTTP.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import app, get_reports
from shortcut.crowding import (
    CROWD_TTL_MINUTES,
    CROWD_WAIT_CAP_SECONDS,
    CROWD_WAIT_STEP_SECONDS,
    crowd_waits,
    with_crowding,
)
from shortcut.graph_store import CampusGraph
from shortcut.report_store import Report, ReportStore
from shortcut.tools.astar import edge_seconds, find_route

NOW = datetime(2026, 9, 5, 13, 0, tzinfo=timezone.utc)

# The Courtyard sits on the quick way from Staircase 1 to Staircase 3, and it
# has another way round. That is what makes it worth reporting.
CROWDED_NODE = "Hive_B5_G"
QUICK_WAY = ("Hive_B5_A", "Hive_B5_G", "Hive_B5_C")


def report(
    target_id: str,
    *,
    kind: str = "node",
    condition: str = "crowded",
    minutes_ago: float = 0,
    status: str = "pending",
) -> Report:
    return Report(
        id=f"r{target_id}{minutes_ago}{status}",
        target_kind=kind,
        target_id=target_id,
        condition=condition,
        notes="",
        status=status,
        submitted_at=(NOW - timedelta(minutes=minutes_ago)).isoformat(),
    )


# --------------------------------------------------------------------------
# How much slower
# --------------------------------------------------------------------------


def test_nobody_reporting_anything_costs_nothing(graph: CampusGraph) -> None:
    assert crowd_waits(graph, [], NOW) == {}


def test_one_report_slows_every_way_through_that_place(
    graph: CampusGraph,
) -> None:
    """A packed lobby is in the way of everything that goes through it."""
    waits = crowd_waits(graph, [report(CROWDED_NODE)], NOW)

    touching = {edge.id for edge in graph.edges_from(CROWDED_NODE)}
    assert set(waits) == touching
    assert set(waits.values()) == {CROWD_WAIT_STEP_SECONDS}


def test_reports_of_the_same_place_stack(graph: CampusGraph) -> None:
    waits = crowd_waits(graph, [report(CROWDED_NODE), report(CROWDED_NODE)], NOW)

    assert set(waits.values()) == {CROWD_WAIT_STEP_SECONDS * 2}


def test_the_extra_wait_is_capped(graph: CampusGraph) -> None:
    """However bad it looks, a lift is slow rather than impassable."""
    waits = crowd_waits(graph, [report(CROWDED_NODE) for _ in range(20)], NOW)

    assert set(waits.values()) == {CROWD_WAIT_CAP_SECONDS}


def test_reporting_one_stretch_slows_only_that_stretch(
    graph: CampusGraph,
) -> None:
    waits = crowd_waits(graph, [report("Hive_B5_002", kind="edge")], NOW)

    assert waits == {"Hive_B5_002": CROWD_WAIT_STEP_SECONDS}


# --------------------------------------------------------------------------
# What gets counted
# --------------------------------------------------------------------------


def test_a_report_is_forgotten_once_it_is_old(graph: CampusGraph) -> None:
    """The lunch rush must not still be happening at four o'clock."""
    stale = report(CROWDED_NODE, minutes_ago=CROWD_TTL_MINUTES + 1)

    assert crowd_waits(graph, [stale], NOW) == {}


def test_a_report_still_counts_just_inside_its_life(graph: CampusGraph) -> None:
    fresh = report(CROWDED_NODE, minutes_ago=CROWD_TTL_MINUTES - 1)

    assert crowd_waits(graph, [fresh], NOW) != {}


def test_a_rejected_report_stops_counting_immediately(
    graph: CampusGraph,
) -> None:
    """A moderator saying it was not true should take effect at once."""
    dismissed = report(CROWDED_NODE, status="rejected")

    assert crowd_waits(graph, [dismissed], NOW) == {}


def test_an_approved_report_counts(graph: CampusGraph) -> None:
    assert crowd_waits(graph, [report(CROWDED_NODE, status="approved")], NOW) != {}


def test_other_kinds_of_problem_are_not_crowding(graph: CampusGraph) -> None:
    """Blocked corridors close the map; they do not merely slow it."""
    assert crowd_waits(graph, [report(CROWDED_NODE, condition="blocked")], NOW) == {}


def test_a_report_about_somewhere_unknown_is_ignored(
    graph: CampusGraph,
) -> None:
    assert crowd_waits(graph, [report("Nowhere_At_All")], NOW) == {}


def test_an_unreadable_timestamp_is_ignored_rather_than_guessed(
    graph: CampusGraph,
) -> None:
    """A bad row must never crash a route request, or silently slow one."""
    broken = Report(
        id="r",
        target_kind="node",
        target_id=CROWDED_NODE,
        condition="crowded",
        notes="",
        status="pending",
        submitted_at="not a date",
    )

    assert crowd_waits(graph, [broken], NOW) == {}


# --------------------------------------------------------------------------
# What it does to a route
# --------------------------------------------------------------------------


def test_with_no_crowding_the_cost_function_is_handed_back_untouched() -> None:
    """The common case pays nothing for this feature, not even a call."""
    assert with_crowding(edge_seconds, {}) is edge_seconds


def test_the_quick_way_goes_through_the_courtyard_normally(
    graph: CampusGraph,
) -> None:
    assert find_route(graph, "Hive_B5_A", "Hive_B5_C").node_ids == QUICK_WAY


def test_a_crowded_courtyard_is_routed_around(graph: CampusGraph) -> None:
    waits = crowd_waits(graph, [report(CROWDED_NODE)], NOW)

    route = find_route(
        graph, "Hive_B5_A", "Hive_B5_C", cost=with_crowding(edge_seconds, waits)
    )

    assert route.node_ids != QUICK_WAY
    assert CROWDED_NODE not in route.node_ids


def test_a_crowded_place_you_asked_for_is_still_reachable(
    graph: CampusGraph,
) -> None:
    """Crowding must never be able to strand somewhere."""
    waits = crowd_waits(graph, [report(CROWDED_NODE) for _ in range(20)], NOW)

    route = find_route(
        graph, "Hive_B5_A", CROWDED_NODE, cost=with_crowding(edge_seconds, waits)
    )

    assert route.node_ids[-1] == CROWDED_NODE


def test_a_crowded_lift_loses_to_the_stairs(graph: CampusGraph) -> None:
    """The case this was built for: the lift at ten to the hour."""
    waits = crowd_waits(
        graph, [report("Hive_B5_015", kind="edge") for _ in range(4)], NOW
    )
    cost = with_crowding(edge_seconds, waits)

    step_free = find_route(
        graph, "Hive_B5_D", "Hive_B4_D", cost=cost, edge_filter=lambda e: not e.stairs
    )
    quickest = find_route(graph, "Hive_B5_D", "Hive_B4_D", cost=cost)

    assert any(graph.edge_by_id(e).lift for e in step_free.edge_ids)
    assert not any(graph.edge_by_id(e).lift for e in quickest.edge_ids)


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    store = ReportStore(tmp_path / "reports.json")
    app.dependency_overrides[get_reports] = lambda: store
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def route_nodes(client: TestClient) -> list[str]:
    return client.post(
        "/route", json={"origin": "Hive_B5_A", "destination": "Hive_B5_C"}
    ).json()["nodes"]


def test_one_tap_reporting_needs_no_notes(client: TestClient) -> None:
    """The whole interaction is meant to be a single tap on a busy corridor."""
    response = client.post(
        "/reports",
        json={"target_kind": "node", "target_id": CROWDED_NODE, "condition": "crowded"},
    )

    assert response.status_code == 201


def test_reporting_a_place_busy_changes_the_next_persons_route(
    client: TestClient,
) -> None:
    assert route_nodes(client) == list(QUICK_WAY)

    client.post(
        "/reports",
        json={"target_kind": "node", "target_id": CROWDED_NODE, "condition": "crowded"},
    )

    assert route_nodes(client) != list(QUICK_WAY)

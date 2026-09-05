"""Getting between B5 and B4.

The Hive's two surveyed floors are joined by four staircases and one lift.
That is what makes the step-free preference mean anything: on a single floor
there is nothing to climb, so "avoid stairs" changes no answer at all.

These assertions are deliberately structural. They say *the quickest way
between floors uses stairs* and *refusing stairs finds the lift and costs
more*, never "it takes 29 seconds" - because the lift's wait is a measured
number that will be re-measured, and a test that hardcodes it would go red
for a survey correction rather than for a bug.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from shortcut.api import app
from shortcut.graph_store import CampusGraph
from shortcut.crowding import CROWD_WAIT_CAP_SECONDS, with_crowding
from shortcut.tools.astar import (
    edge_seconds,
    find_route,
    find_route_or_none,
    prefer_lift_cost,
)

ON_B5 = "Hive_B5_D"  # Main Entrance
ON_B4 = "Hive_B4_D"  # Staircase 2, one floor down


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _floor(graph: CampusGraph, floor: str) -> list[str]:
    return [node.id for node in graph.nodes.values() if node.floor == floor]


def _no_stairs(edge) -> bool:
    return not edge.stairs


# --------------------------------------------------------------------------
# The floors are joined
# --------------------------------------------------------------------------


def test_every_place_on_b4_can_be_reached_from_every_place_on_b5(
    graph: CampusGraph,
) -> None:
    """A floor reachable only in places is worse than one nobody surveyed."""
    b5, b4 = _floor(graph, "B5"), _floor(graph, "B4")
    assert b5 and b4

    unreachable = [
        (start, end)
        for start in b5
        for end in b4
        if find_route_or_none(graph, start, end) is None
    ]

    assert unreachable == []


def test_both_floors_are_actually_in_the_survey(graph: CampusGraph) -> None:
    assert len(_floor(graph, "B5")) == 9
    assert len(_floor(graph, "B4")) == 8


# --------------------------------------------------------------------------
# Stairs, lift, and the trade between them
# --------------------------------------------------------------------------


def test_the_quickest_way_between_floors_takes_the_stairs(
    graph: CampusGraph,
) -> None:
    route = find_route(graph, ON_B5, ON_B4)

    assert any(graph.edge_by_id(edge_id).stairs for edge_id in route.edge_ids)


def test_ruling_out_stairs_finds_the_lift_instead(graph: CampusGraph) -> None:
    route = find_route(graph, ON_B5, ON_B4, edge_filter=_no_stairs)

    assert any(graph.edge_by_id(edge_id).lift for edge_id in route.edge_ids)
    assert not any(graph.edge_by_id(edge_id).stairs for edge_id in route.edge_ids)


def test_going_step_free_costs_real_time(graph: CampusGraph) -> None:
    """Worth stating plainly on screen, rather than hiding the difference."""
    stairs = find_route(graph, ON_B5, ON_B4)
    step_free = find_route(graph, ON_B5, ON_B4, edge_filter=_no_stairs)

    assert step_free.total_seconds > stairs.total_seconds


def test_the_lift_makes_you_wait_and_we_report_it_separately(
    graph: CampusGraph,
) -> None:
    """Waiting is not walking, so it is counted apart from walking time."""
    step_free = find_route(graph, ON_B5, ON_B4, edge_filter=_no_stairs)

    waits = sum(graph.edge_by_id(edge_id).wait_seconds for edge_id in step_free.edge_ids)
    assert waits > 0


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------


def test_a_step_free_request_crosses_floors_by_lift(client: TestClient) -> None:
    body = client.post(
        "/route",
        json={"origin": ON_B5, "destination": ON_B4, "allow_stairs": False},
    ).json()

    assert body["uses_lift"] is True
    assert body["uses_stairs"] is False
    assert body["total_wait_seconds"] > 0


def test_refusing_both_stairs_and_lift_cannot_change_floor(
    client: TestClient,
) -> None:
    """There is no third way down, and saying so beats inventing one."""
    response = client.post(
        "/route",
        json={
            "origin": ON_B5,
            "destination": ON_B4,
            "allow_stairs": False,
            "allow_lift": False,
        },
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------
# The preference has to survive the conditions
# --------------------------------------------------------------------------
#
# These run against the *real* graph on purpose. The stairs-and-lift tests in
# tests/test_api.py use a synthetic pair where the lift is only 25 seconds
# dearer than the stairs, which is a smaller gap than any real building has -
# so a stairs penalty could look correct there while being far too small to
# work here. It was. This is the test that would have caught it.


def test_asking_for_less_climbing_actually_takes_the_lift(
    graph: CampusGraph,
) -> None:
    """The Hive's lift is 87 seconds dearer than the stairs beside it.

    Someone asking to avoid stairs often cannot use them at all, so the
    preference has to outweigh the detour rather than merely lean against it.
    """
    route = find_route(graph, ON_B5, ON_B4, cost=prefer_lift_cost())

    assert any(graph.edge_by_id(edge_id).lift for edge_id in route.edge_ids)
    assert not any(graph.edge_by_id(edge_id).stairs for edge_id in route.edge_ids)


def test_a_packed_lift_does_not_send_a_step_free_route_up_the_stairs(
    graph: CampusGraph,
) -> None:
    """Someone who asked to avoid stairs has already accepted the wait.

    A queue is a reason to warn them, never a reason to route them onto a
    staircase they may not be able to climb.
    """
    packed = {"Hive_B5_015": CROWD_WAIT_CAP_SECONDS}

    route = find_route(
        graph, ON_B5, ON_B4, cost=with_crowding(prefer_lift_cost(), packed)
    )

    assert any(graph.edge_by_id(edge_id).lift for edge_id in route.edge_ids)


def test_a_packed_lift_does_send_someone_in_a_hurry_up_the_stairs(
    graph: CampusGraph,
) -> None:
    """The other half of the same rule: 'fastest' means fastest right now."""
    packed = {"Hive_B5_015": CROWD_WAIT_CAP_SECONDS}

    route = find_route(graph, ON_B5, ON_B4, cost=with_crowding(edge_seconds, packed))

    assert any(graph.edge_by_id(edge_id).stairs for edge_id in route.edge_ids)


def test_stairs_are_still_used_when_the_lift_is_the_one_that_is_shut(
    graph: CampusGraph,
) -> None:
    """The penalty is large, not infinite. No lift, no route, would be worse."""
    route = find_route(
        graph,
        ON_B5,
        ON_B4,
        cost=prefer_lift_cost(),
        edge_filter=lambda edge: not edge.lift,
    )

    assert any(graph.edge_by_id(edge_id).stairs for edge_id in route.edge_ids)


# --------------------------------------------------------------------------
# Offering the other kind of route
# --------------------------------------------------------------------------
#
# A route can be worth showing because of what it *is*, not only because it
# wins on a number. The step-free way between floors is slower and walks you
# further, so it loses on every measure the search scores by - and it is
# still the one a person with a suitcase or a bad knee needs to see.


def options(client: TestClient, **overrides) -> dict:
    payload = {"origin": ON_B5, "destination": ON_B4}
    payload.update(overrides)
    return client.post("/route/options", json=payload).json()


def labels(body: dict) -> list[str]:
    return [option["label"] for option in body["alternatives"]]


def test_asking_for_the_quickest_way_still_shows_the_step_free_one(
    client: TestClient,
) -> None:
    body = options(client)

    assert body["primary"]["route"]["uses_stairs"] is True
    assert "Step-free" in labels(body)


def test_the_step_free_alternative_really_is_step_free(
    client: TestClient,
) -> None:
    body = options(client)
    step_free = next(o for o in body["alternatives"] if o["label"] == "Step-free")

    assert step_free["route"]["uses_stairs"] is False
    assert step_free["route"]["uses_lift"] is True


def test_it_says_what_the_step_free_way_costs(client: TestClient) -> None:
    """A choice without the trade written next to it is not a choice."""
    body = options(client)
    step_free = next(o for o in body["alternatives"] if o["label"] == "Step-free")

    assert "slower" in step_free["why"]


def test_the_option_asked_for_comes_first(client: TestClient) -> None:
    """Ranking is the whole point: the top one should need no explaining."""
    quick = options(client)
    climbing = options(client, preference="prefer_lift")

    assert quick["primary"]["route"]["uses_stairs"] is True
    assert climbing["primary"]["route"]["uses_lift"] is True


def test_a_step_free_route_is_not_offered_as_an_alternative_to_itself(
    client: TestClient,
) -> None:
    """It is already the answer, so repeating it would be noise."""
    body = options(client, preference="prefer_lift")

    assert "Step-free" not in labels(body)


def test_nothing_is_offered_twice(client: TestClient) -> None:
    body = options(client)
    routes = [body["primary"]["route"]["nodes"]] + [
        option["route"]["nodes"] for option in body["alternatives"]
    ]

    assert len(routes) == len({tuple(nodes) for nodes in routes})

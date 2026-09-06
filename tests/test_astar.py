"""Tests for the deterministic route search in ``shortcut.tools.astar``.

These tests run against the real ``data/campus_graph.json``. Where a test needs
different data (a blocked corridor, a deliberately disconnected building) it
writes a *modified copy* into pytest's ``tmp_path``; the real file on disk is
never edited. ``test_real_graph_file_is_never_modified`` enforces that.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Callable, Iterable

import pytest

from shortcut.graph_store import CampusGraph, Edge, load_graph, UnknownNodeError
from shortcut.tools.astar import (
    NoRouteFoundError,
    Route,
    find_route,
    find_route_or_none,
)

# The route under test, worked out by hand from the floorplan. It stays on B5
# even though B4 is surveyed too: every way down and back costs more than a
# minute, so the cross-floor edges cannot shorten it. That is deliberate - the
# golden route should pin the search, not the size of the survey.
#   Main Staircase --(001)-> Lift Lobby --(002)-> Courtyard --(015)-> Main Entrance
ORIGIN = "Hive_B5_B"
DESTINATION = "Hive_B5_I"
EXPECTED_NODES = ("Hive_B5_B", "Hive_B5_A", "Hive_B5_G", "Hive_B5_I")
EXPECTED_EDGES = ("Hive_B5_001", "Hive_B5_002", "Hive_B5_015")


# --------------------------------------------------------------------------
# Helpers and fixtures
# --------------------------------------------------------------------------


def edge_by_id(graph: CampusGraph, edge_id: str) -> Edge:
    """Look up one edge by its id, so tests can read costs from the graph."""
    for edge in graph.edges:
        if edge.id == edge_id:
            return edge
    raise AssertionError(f"Test bug: no edge {edge_id!r} in the graph.")


def total_seconds_of(graph: CampusGraph, edge_ids: Iterable[str]) -> float:
    """Sum walking seconds straight from the graph, not from hard-coded numbers."""
    return sum(edge_by_id(graph, edge_id).walk_seconds for edge_id in edge_ids)


def write_graph(directory: Path, data: dict, name: str = "graph.json") -> Path:
    """Write a graph dictionary to a temporary JSON file and return its path."""
    path = directory / name
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def graph_with_blocked_edges(
    graph_path: Path,
    tmp_path: Path,
) -> Callable[[set[str]], CampusGraph]:
    """Factory: build a temporary graph with the named edges marked blocked.

    The real ``campus_graph.json`` is read, deep-copied, modified in memory and
    written to ``tmp_path``. The original file is left untouched.
    """
    original = json.loads(graph_path.read_text(encoding="utf-8"))

    def build(blocked_edge_ids: set[str]) -> CampusGraph:
        data = copy.deepcopy(original)
        known_ids = {edge["id"] for edge in data["edges"]}
        unknown = blocked_edge_ids - known_ids
        assert not unknown, f"Test bug: no such edge ids in the graph: {unknown}"

        for edge in data["edges"]:
            if edge["id"] in blocked_edge_ids:
                edge["blocked"] = True

        name = "blocked_" + "_".join(sorted(blocked_edge_ids)) + ".json"
        return load_graph(write_graph(tmp_path, data, name))

    return build


@pytest.fixture
def two_island_graph(tmp_path: Path) -> CampusGraph:
    """A tiny graph in two disconnected halves: A-B and Y-Z."""
    data = {
        "nodes": [
            {"id": "A", "name": "Island 1 start", "building": "Test", "floor": "L1", "type": "junction"},
            {"id": "B", "name": "Island 1 end", "building": "Test", "floor": "L1", "type": "junction"},
            {"id": "Y", "name": "Island 2 start", "building": "Test", "floor": "L1", "type": "junction"},
            {"id": "Z", "name": "Island 2 end", "building": "Test", "floor": "L1", "type": "junction"},
        ],
        "edges": [
            {
                "id": "AB",
                "from": "A",
                "to": "B",
                "distance_m": 10,
                "walk_seconds": 8,
                "covered": True,
                "stairs": False,
                "lift": False,
                "blocked": False,
            },
            {
                "id": "YZ",
                "from": "Y",
                "to": "Z",
                "distance_m": 10,
                "walk_seconds": 8,
                "covered": True,
                "stairs": False,
                "lift": False,
                "blocked": False,
            },
        ],
    }
    return load_graph(write_graph(tmp_path, data, "two_islands.json"))


# --------------------------------------------------------------------------
# A successful route: Hive_B5_A -> Hive_B5_C
# --------------------------------------------------------------------------


def test_route_is_found(graph: CampusGraph) -> None:
    route = find_route(graph, ORIGIN, DESTINATION)

    assert isinstance(route, Route)
    assert route.origin == ORIGIN
    assert route.destination == DESTINATION


def test_node_sequence_is_correct(graph: CampusGraph) -> None:
    route = find_route(graph, ORIGIN, DESTINATION)

    assert route.node_ids == EXPECTED_NODES


def test_edge_sequence_is_correct(graph: CampusGraph) -> None:
    route = find_route(graph, ORIGIN, DESTINATION)

    assert route.edge_ids == EXPECTED_EDGES
    assert route.step_count == len(EXPECTED_NODES) - 1


def test_total_walking_time_matches_graph_values(graph: CampusGraph) -> None:
    """The reported total must equal the sum of the edges' own walk_seconds."""
    route = find_route(graph, ORIGIN, DESTINATION)

    expected_seconds = total_seconds_of(graph, EXPECTED_EDGES)

    assert route.total_seconds == pytest.approx(expected_seconds)
    assert route.total_seconds == pytest.approx(24.0)  # 3 + 9 + 12


def test_total_distance_matches_graph_values(graph: CampusGraph) -> None:
    route = find_route(graph, ORIGIN, DESTINATION)

    expected_metres = sum(edge_by_id(graph, e).distance_m for e in EXPECTED_EDGES)

    assert route.total_distance_m == pytest.approx(expected_metres)
    assert route.total_distance_m == pytest.approx(33.6)  # 4.2 + 12.6 + 16.8


def test_route_edges_actually_join_the_nodes(graph: CampusGraph) -> None:
    """Every reported edge must connect the two nodes it sits between."""
    route = find_route(graph, ORIGIN, DESTINATION)

    for index, edge_id in enumerate(route.edge_ids):
        edge = edge_by_id(graph, edge_id)
        here, next_node = route.node_ids[index], route.node_ids[index + 1]
        assert {edge.from_id, edge.to_id} == {here, next_node}


def test_route_reports_stairs_and_lift_use(graph: CampusGraph) -> None:
    """With only one floor surveyed, nothing here uses stairs or a lift.

    Both flags only ever lived on the corridors that crossed floors, so a
    single-floor graph has neither - real reflection of the current data,
    not a broken test. See test_a_stairs_edge_is_reported / test_a_lift_edge_is_reported
    below for coverage of the flags actually working, against a small
    synthetic graph built for exactly that.
    """
    route = find_route(graph, ORIGIN, DESTINATION)

    assert route.uses_stairs is False
    assert route.uses_lift is False


def test_a_stairs_edge_is_reported(tmp_path: Path) -> None:
    data = {
        "nodes": [
            {"id": "A", "name": "Upper", "building": "Test", "floor": "1", "type": "stairs"},
            {"id": "B", "name": "Lower", "building": "Test", "floor": "2", "type": "stairs"},
        ],
        "edges": [
            {
                "id": "AB", "from": "A", "to": "B",
                "distance_m": 15, "walk_seconds": 20,
                "covered": True, "stairs": True, "lift": False, "blocked": False,
            },
        ],
    }
    graph = load_graph(write_graph(tmp_path, data, "stairs_only.json"))

    route = find_route(graph, "A", "B")

    assert route.uses_stairs is True
    assert route.uses_lift is False


def test_a_lift_edge_is_reported(tmp_path: Path) -> None:
    data = {
        "nodes": [
            {"id": "A", "name": "Upper", "building": "Test", "floor": "1", "type": "lift"},
            {"id": "B", "name": "Lower", "building": "Test", "floor": "2", "type": "lift"},
        ],
        "edges": [
            {
                "id": "AB", "from": "A", "to": "B",
                "distance_m": 0, "walk_seconds": 5,
                "covered": True, "stairs": False, "lift": True, "blocked": False,
            },
        ],
    }
    graph = load_graph(write_graph(tmp_path, data, "lift_only.json"))

    route = find_route(graph, "A", "B")

    assert route.uses_lift is True
    assert route.uses_stairs is False


def test_route_is_cheaper_than_a_known_alternative(graph: CampusGraph) -> None:
    """Sanity check that the search really minimises, rather than just walking."""
    route = find_route(graph, ORIGIN, DESTINATION)
    alternative = ("Hive_B5_004", "Hive_B5_010", "Hive_B5_012")  # via Pick Lockers

    assert route.total_seconds < total_seconds_of(graph, alternative)


def test_search_is_deterministic(graph: CampusGraph) -> None:
    """Repeated runs must return exactly the same route, every time."""
    first = find_route(graph, ORIGIN, DESTINATION)

    for _ in range(20):
        again = find_route(graph, ORIGIN, DESTINATION)
        assert again.node_ids == first.node_ids
        assert again.edge_ids == first.edge_ids
        assert again.total_seconds == first.total_seconds


def test_reverse_route_mirrors_the_forward_route(graph: CampusGraph) -> None:
    """Edges are two-way, so walking back must cost the same."""
    forward = find_route(graph, ORIGIN, DESTINATION)
    backward = find_route(graph, DESTINATION, ORIGIN)

    assert backward.node_ids == tuple(reversed(forward.node_ids))
    assert backward.total_seconds == pytest.approx(forward.total_seconds)


def test_route_from_a_node_to_itself_is_empty(graph: CampusGraph) -> None:
    route = find_route(graph, ORIGIN, ORIGIN)

    assert route.node_ids == (ORIGIN,)
    assert route.edge_ids == ()
    assert route.total_seconds == 0
    assert route.total_distance_m == 0


# --------------------------------------------------------------------------
# Blocked edges
# --------------------------------------------------------------------------


def test_blocked_edge_is_avoided_when_an_alternative_exists(
    graph: CampusGraph,
    graph_with_blocked_edges: Callable[[set[str]], CampusGraph],
) -> None:
    """Blocking the first hop must push the route onto a different, valid path."""
    blocked_id = EXPECTED_EDGES[0]  # Hive_B5_001, Main Staircase -> Lift Lobby
    detour_graph = graph_with_blocked_edges({blocked_id})

    detour = find_route(detour_graph, ORIGIN, DESTINATION)
    original = find_route(graph, ORIGIN, DESTINATION)

    assert blocked_id not in detour.edge_ids
    assert detour.origin == ORIGIN and detour.destination == DESTINATION
    # A detour can never be cheaper than the unrestricted best route.
    assert detour.total_seconds > original.total_seconds


def test_no_blocked_edge_ever_appears_in_a_route(
    graph_with_blocked_edges: Callable[[set[str]], CampusGraph],
) -> None:
    """Whatever path is chosen, none of its edges may be marked blocked."""
    detour_graph = graph_with_blocked_edges({"Hive_B5_001", "Hive_B5_002"})

    route = find_route(detour_graph, ORIGIN, DESTINATION)

    for edge_id in route.edge_ids:
        assert edge_by_id(detour_graph, edge_id).blocked is False


def test_blocking_every_edge_of_a_node_makes_it_unreachable(
    graph_with_blocked_edges: Callable[[set[str]], CampusGraph],
) -> None:
    """The Main Staircase has three ways out: two corridors and its own stairs.

    Block all three and it is stranded, which is the point: a place is only
    unreachable once every edge touching it is closed.
    """
    cut_off_graph = graph_with_blocked_edges(
        {"Hive_B5_001", "Hive_B5_004", "Hive_Stairs_B"}
    )

    assert cut_off_graph.neighbours("Hive_B5_B") == []
    with pytest.raises(NoRouteFoundError):
        find_route(cut_off_graph, "Hive_B5_G", "Hive_B5_B")


# --------------------------------------------------------------------------
# Invalid node ids
# --------------------------------------------------------------------------


def test_unknown_origin_raises(graph: CampusGraph) -> None:
    with pytest.raises(UnknownNodeError) as error:
        find_route(graph, "Hive_B9_NOPE", DESTINATION)

    assert "Hive_B9_NOPE" in str(error.value)


def test_unknown_destination_raises(graph: CampusGraph) -> None:
    with pytest.raises(UnknownNodeError) as error:
        find_route(graph, ORIGIN, "Hive_B9_NOPE")

    assert "Hive_B9_NOPE" in str(error.value)


def test_unknown_origin_is_reported_even_when_both_ids_are_bad(
    graph: CampusGraph,
) -> None:
    """A missing node must never be mistaken for an origin == destination route."""
    with pytest.raises(UnknownNodeError):
        find_route(graph, "Hive_B9_NOPE", "Hive_B9_NOPE")


def test_find_route_or_none_returns_none_for_unknown_nodes(graph: CampusGraph) -> None:
    assert find_route_or_none(graph, ORIGIN, "Hive_B9_NOPE") is None
    assert find_route_or_none(graph, "Hive_B9_NOPE", DESTINATION) is None


# --------------------------------------------------------------------------
# No route
# --------------------------------------------------------------------------


def test_disconnected_nodes_raise_no_route_found(two_island_graph: CampusGraph) -> None:
    with pytest.raises(NoRouteFoundError) as error:
        find_route(two_island_graph, "A", "Z")

    message = str(error.value)
    assert "A" in message and "Z" in message


def test_no_route_error_carries_both_node_ids(two_island_graph: CampusGraph) -> None:
    with pytest.raises(NoRouteFoundError) as error:
        find_route(two_island_graph, "B", "Y")

    assert error.value.origin == "B"
    assert error.value.destination == "Y"


def test_find_route_or_none_returns_none_when_no_route_exists(
    two_island_graph: CampusGraph,
) -> None:
    assert find_route_or_none(two_island_graph, "A", "Z") is None


def test_routes_within_one_island_still_work(two_island_graph: CampusGraph) -> None:
    """Make sure the island fixture is genuinely split, not simply broken."""
    route = find_route(two_island_graph, "A", "B")

    assert route.node_ids == ("A", "B")
    assert route.total_seconds == 8


# --------------------------------------------------------------------------
# Shelter
# --------------------------------------------------------------------------


def test_real_graph_routes_are_fully_sheltered(graph: CampusGraph) -> None:
    """Every edge in campus_graph.json is covered, so any route is sheltered."""
    route = find_route(graph, ORIGIN, DESTINATION)

    assert route.fully_sheltered is True


def test_one_uncovered_edge_makes_the_whole_route_unsheltered(
    graph_path: Path,
    tmp_path: Path,
) -> None:
    """Uses "all", not "any": one open-air stretch counts against the route."""
    data = copy.deepcopy(json.loads(graph_path.read_text(encoding="utf-8")))
    for edge in data["edges"]:
        if edge["id"] == EXPECTED_EDGES[0]:
            edge["covered"] = False
    open_air_graph = load_graph(write_graph(tmp_path, data, "open_air.json"))

    route = find_route(open_air_graph, ORIGIN, DESTINATION)

    assert EXPECTED_EDGES[0] in route.edge_ids, "expected the same path as before"
    assert route.fully_sheltered is False


def test_a_route_to_the_same_node_counts_as_sheltered(graph: CampusGraph) -> None:
    """Walking no edges at all means no exposure to the weather."""
    route = find_route(graph, ORIGIN, ORIGIN)

    assert route.fully_sheltered is True


# --------------------------------------------------------------------------
# Safety net: the committed graph file must stay untouched
# --------------------------------------------------------------------------


def test_real_graph_file_is_never_modified(
    graph_path: Path, graph_bytes_at_session_start: bytes
) -> None:
    """Guard for requirement 9: tests must not rewrite data/campus_graph.json.

    Compares against a snapshot taken before any test ran, not a hardcoded
    count: the graph is real survey data that keeps growing, so a fixed
    node/edge number would go stale the moment someone surveys a new place.
    """
    assert graph_path.read_bytes() == graph_bytes_at_session_start

    data = json.loads(graph_path.read_text(encoding="utf-8"))
    assert all(edge.get("blocked", False) is False for edge in data["edges"])


# --------------------------------------------------------------------------
# Coordinates must speed the search up without ever changing its answer
# --------------------------------------------------------------------------


def test_giving_places_coordinates_never_changes_a_single_route(
    graph_path: Path, tmp_path: Path
) -> None:
    """Every trip on the real map, with the coordinates and without them.

    A* only returns the shortest route while its straight-line estimate stays
    both admissible and consistent, and coordinates are what let it estimate
    at all. They are also traced per floor, onto separate plans with separate
    origins, so a distance measured across two of them means nothing - and an
    estimate built on that is neither.

    This is the check that says so out loud, because the failure is invisible:
    no error, no warning, just a route a few seconds longer than the one that
    existed. When it was first written it failed on 76 of the 1560 trips
    below, and the fix was to leave the estimate out whenever the map spans
    more than one plan.
    """
    surveyed = json.loads(graph_path.read_text(encoding="utf-8"))
    assert any("x" in node for node in surveyed["nodes"]), "no floor is traced yet"

    flattened = copy.deepcopy(surveyed)
    for node in flattened["nodes"]:
        node.pop("x", None)
        node.pop("y", None)
    without_path = tmp_path / "no_coordinates.json"
    without_path.write_text(json.dumps(flattened), encoding="utf-8")

    with_coordinates = load_graph(graph_path)
    without_coordinates = load_graph(without_path)

    differed: list[str] = []
    for origin in sorted(with_coordinates.nodes):
        for destination in sorted(with_coordinates.nodes):
            if origin == destination:
                continue
            here = find_route_or_none(with_coordinates, origin, destination)
            there = find_route_or_none(without_coordinates, origin, destination)
            if (here is None) != (there is None):
                differed.append(f"{origin} -> {destination}: one found a route, one did not")
            elif here is not None and there is not None and here.node_ids != there.node_ids:
                differed.append(
                    f"{origin} -> {destination}: {here.total_seconds}s with "
                    f"coordinates, {there.total_seconds}s without"
                )

    assert differed == []

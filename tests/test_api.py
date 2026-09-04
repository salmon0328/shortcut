"""Tests for the FastAPI layer in ``shortcut.api``.

These tests drive the API the way a real client would: they send HTTP requests
through FastAPI's ``TestClient`` and check the JSON that comes back. No server
needs to be running, and no network is used.

The import below is ``shortcut.api``, not ``src.shortcut.api``: ``src`` is a
plain folder, not a package, and ``tests/conftest.py`` already puts ``src`` on
the import path. This is the same reason uvicorn needs ``--app-dir src``.

Nothing here calls AWS, Bedrock or Claude, and no test edits the real
``data/campus_graph.json``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import DEV_ALLOWED_ORIGINS, app, get_graph
from shortcut.graph_store import CampusGraph, load_graph

# The route under test, taken from the real graph:
#   Hive_B5_A --(Hive_B5_017, stairs)-> Hive_B4_A
#              --(Hive_B4_002)-> Hive_B4_F --(Hive_B4_007)-> Hive_B4_E
ORIGIN = "Hive_B5_A"
DESTINATION = "Hive_B4_E"
EXPECTED_NODES = ["Hive_B5_A", "Hive_B4_A", "Hive_B4_F", "Hive_B4_E"]
EXPECTED_EDGES = ["Hive_B5_017", "Hive_B4_002", "Hive_B4_007"]
EXPECTED_SECONDS = 36.0  # 20 + 8 + 8
EXPECTED_METRES = 37.4  # 15.0 + 11.2 + 11.2


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A test client with the app's startup code run.

    ``TestClient`` must be used as a context manager (``with ...``). Only then
    does FastAPI run the ``lifespan`` handler that loads the campus graph;
    without it every ``/route`` request would fail with 503.
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def cut_off_graph(graph_path: Path, tmp_path: Path) -> CampusGraph:
    """A temporary graph where Hive_B5_B has both of its edges blocked.

    The real JSON is read, copied in memory, changed, and written to
    ``tmp_path``. The file in ``data/`` is never touched.
    """
    data = copy.deepcopy(json.loads(graph_path.read_text(encoding="utf-8")))
    for edge in data["edges"]:
        if edge["id"] in {"Hive_B5_004", "Hive_B5_015"}:
            edge["blocked"] = True

    temporary_file = tmp_path / "cut_off_graph.json"
    temporary_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return load_graph(temporary_file)


@pytest.fixture
def cut_off_client(cut_off_graph: CampusGraph) -> Iterator[TestClient]:
    """A client that serves the cut-off graph instead of the real one.

    ``dependency_overrides`` swaps out :func:`shortcut.api.get_graph`, which is
    why that function exists as a dependency rather than as a global.
    """
    app.dependency_overrides[get_graph] = lambda: cut_off_graph
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def post_route(client: TestClient, origin: str, destination: str):
    """Send one POST /route request."""
    return client.post("/route", json={"origin": origin, "destination": destination})


def post_route_with_origin(client: TestClient, browser_origin: str):
    """Send the standard POST /route request as if from a browser page.

    ``browser_origin`` is the web address of the calling page, which is what
    CORS checks. It is unrelated to the route's ``origin`` node.
    """
    return client.post(
        "/route",
        json={"origin": ORIGIN, "destination": DESTINATION},
        headers={"Origin": browser_origin},
    )


# --------------------------------------------------------------------------
# GET /health
# --------------------------------------------------------------------------


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --------------------------------------------------------------------------
# GET /nodes
# --------------------------------------------------------------------------


def test_nodes_returns_200(client: TestClient) -> None:
    response = client.get("/nodes")

    assert response.status_code == 200


def test_nodes_returns_every_node_in_the_graph(
    client: TestClient, graph: CampusGraph
) -> None:
    """Guards against exactly the problem this endpoint exists to solve:
    the API's node list drifting out of sync with the real graph."""
    body = client.get("/nodes").json()

    assert len(body) == len(graph.nodes)
    assert {node["id"] for node in body} == set(graph.nodes)


def test_each_node_has_the_expected_shape(client: TestClient) -> None:
    body = client.get("/nodes").json()

    for node in body:
        assert set(node) == {"id", "name", "building", "floor", "condition"}
        assert isinstance(node["id"], str) and node["id"]
        assert isinstance(node["name"], str) and node["name"]
        assert isinstance(node["building"], str) and node["building"]
        assert isinstance(node["floor"], str) and node["floor"]


def test_every_node_matches_the_graph_exactly(
    client: TestClient, graph: CampusGraph
) -> None:
    """Compare every field against the graph, not just the ids.

    Checking only ids would let a mapping bug through: if ``from_node`` sent
    the id as the name, or dropped the building, the ids would still line up.
    """
    body = client.get("/nodes").json()

    for summary in body:
        node = graph.nodes[summary["id"]]
        assert summary["name"] == node.name
        assert summary["building"] == node.building
        assert summary["floor"] == node.floor


def test_a_known_node_carries_its_real_name_and_building(
    client: TestClient,
) -> None:
    body = client.get("/nodes").json()

    by_id = {node["id"]: node for node in body}
    assert by_id[ORIGIN] == {
        "id": "Hive_B5_A",
        "name": "Staircase 1",
        "building": "Hive",
        "floor": "B5",
        "condition": None,
    }
    assert by_id[DESTINATION]["name"] == "Side Entrance"
    assert by_id[DESTINATION]["floor"] == "B4"


def test_nodes_response_is_a_plain_list_not_wrapped_in_an_object(
    client: TestClient,
) -> None:
    """A frontend expects to iterate the response directly."""
    response = client.get("/nodes")

    assert isinstance(response.json(), list)


# --------------------------------------------------------------------------
# GET /edges
# --------------------------------------------------------------------------


def test_edges_returns_every_edge_in_the_graph(
    client: TestClient, graph: CampusGraph
) -> None:
    body = client.get("/edges").json()

    assert len(body) == len(graph.edges)
    assert {edge["id"] for edge in body} == {edge.id for edge in graph.edges}


def test_each_edge_has_the_expected_shape(client: TestClient) -> None:
    body = client.get("/edges").json()

    for edge in body:
        assert set(edge) == {
            "id",
            "from_id",
            "to_id",
            "label",
            "distance_m",
            "walk_seconds",
            "covered",
            "stairs",
            "lift",
            "blocked",
            "condition",
        }


def test_an_edge_is_labelled_with_both_ends(client: TestClient) -> None:
    """The report queue shows this, so it has to read as a place, not an id."""
    by_id = {edge["id"]: edge for edge in client.get("/edges").json()}

    assert by_id["Hive_B5_002"]["label"] == "Staircase 1 → Courtyard"


def test_edge_endpoints_are_real_nodes(
    client: TestClient, graph: CampusGraph
) -> None:
    for edge in client.get("/edges").json():
        assert edge["from_id"] in graph.nodes
        assert edge["to_id"] in graph.nodes


# --------------------------------------------------------------------------
# POST /route: the successful case
# --------------------------------------------------------------------------


def test_route_returns_200(client: TestClient) -> None:
    response = post_route(client, ORIGIN, DESTINATION)

    assert response.status_code == 200


def test_route_response_contains_nodes_and_walking_time(client: TestClient) -> None:
    """The two fields a caller most needs must always be present."""
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert "nodes" in body
    assert "total_walk_seconds" in body
    assert body["nodes"], "the route must list at least one node"


def test_route_response_matches_the_schema(client: TestClient) -> None:
    """Exactly the fields RouteResponse defines, no more and no fewer."""
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert set(body) == {
        "nodes",
        "edges",
        "steps",
        "total_distance_m",
        "total_walk_seconds",
        "uses_stairs",
        "uses_lift",
        "fully_sheltered",
    }


def test_route_node_sequence_is_correct(client: TestClient) -> None:
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert body["nodes"] == EXPECTED_NODES
    assert body["nodes"][0] == ORIGIN
    assert body["nodes"][-1] == DESTINATION


def test_route_edge_sequence_is_correct(client: TestClient) -> None:
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert body["edges"] == EXPECTED_EDGES


def test_route_total_walking_time_is_correct(client: TestClient) -> None:
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert body["total_walk_seconds"] == pytest.approx(EXPECTED_SECONDS)


def test_route_total_distance_is_correct(client: TestClient) -> None:
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert body["total_distance_m"] == pytest.approx(EXPECTED_METRES)


def test_route_reports_stairs_but_not_lift(client: TestClient) -> None:
    """Hive_B5_017 is the staircase between floors B5 and B4."""
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert body["uses_stairs"] is True
    assert body["uses_lift"] is False


def test_route_reports_shelter(client: TestClient) -> None:
    """Every edge in the real graph is covered, so this route is sheltered."""
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert body["fully_sheltered"] is True


def test_route_has_one_fewer_edge_than_nodes(client: TestClient) -> None:
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert len(body["edges"]) == len(body["nodes"]) - 1


def test_route_totals_are_never_negative(client: TestClient) -> None:
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert body["total_walk_seconds"] >= 0
    assert body["total_distance_m"] >= 0


def test_repeated_requests_return_the_same_route(client: TestClient) -> None:
    """The router is deterministic, so the API must be too."""
    first = post_route(client, ORIGIN, DESTINATION).json()

    for _ in range(5):
        assert post_route(client, ORIGIN, DESTINATION).json() == first


def test_surrounding_whitespace_is_ignored(client: TestClient) -> None:
    """RouteRequest strips whitespace, so a padded id still resolves."""
    response = post_route(client, f"  {ORIGIN}  ", DESTINATION)

    assert response.status_code == 200
    assert response.json()["nodes"] == EXPECTED_NODES


def test_route_to_the_same_node_is_empty_but_valid(client: TestClient) -> None:
    response = post_route(client, ORIGIN, ORIGIN)

    assert response.status_code == 200
    body = response.json()
    assert body["nodes"] == [ORIGIN]
    assert body["edges"] == []
    assert body["total_walk_seconds"] == 0
    assert body["total_distance_m"] == 0


# --------------------------------------------------------------------------
# POST /route: the step-by-step directions
# --------------------------------------------------------------------------


def test_there_is_one_step_per_edge(client: TestClient) -> None:
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert len(body["steps"]) == len(body["edges"])
    assert len(body["steps"]) == len(body["nodes"]) - 1


def test_steps_are_numbered_from_one_in_order(client: TestClient) -> None:
    steps = post_route(client, ORIGIN, DESTINATION).json()["steps"]

    assert [step["step"] for step in steps] == [1, 2, 3]


def test_steps_join_up_into_a_continuous_walk(client: TestClient) -> None:
    """Each step must start where the previous one ended."""
    body = post_route(client, ORIGIN, DESTINATION).json()
    steps = body["steps"]

    assert steps[0]["from_id"] == body["nodes"][0]
    assert steps[-1]["to_id"] == body["nodes"][-1]
    for earlier, later in zip(steps, steps[1:]):
        assert earlier["to_id"] == later["from_id"]


def test_every_step_carries_text(client: TestClient) -> None:
    """No step may be blank, even though no directions are written yet."""
    steps = post_route(client, ORIGIN, DESTINATION).json()["steps"]

    for step in steps:
        assert step["instruction"].strip()
        assert step["detail"].strip()


def test_step_numbers_add_up_to_the_route_totals(client: TestClient) -> None:
    body = post_route(client, ORIGIN, DESTINATION).json()

    assert sum(s["distance_m"] for s in body["steps"]) == pytest.approx(
        body["total_distance_m"]
    )
    assert sum(s["walk_seconds"] for s in body["steps"]) == pytest.approx(
        body["total_walk_seconds"]
    )


def test_the_stairs_step_is_described_as_stairs(client: TestClient) -> None:
    steps = post_route(client, ORIGIN, DESTINATION).json()["steps"]

    stairs_steps = [step for step in steps if step["stairs"]]
    assert len(stairs_steps) == 1
    assert "stairs" in stairs_steps[0]["instruction"].lower()
    assert "B4" in stairs_steps[0]["instruction"]


def test_the_lift_step_is_described_as_a_lift(client: TestClient) -> None:
    steps = post_route_with(client, preference="prefer_lift").json()["steps"]

    lift_steps = [step for step in steps if step["lift"]]
    assert len(lift_steps) == 1
    assert "lift" in lift_steps[0]["instruction"].lower()


def test_directions_never_claim_up_or_down(client: TestClient) -> None:
    """Floors are labels like 'B4'; nothing says which way they stack.

    Wording that guessed would be wrong half the time, so it must not appear.
    """
    steps = post_route_with(client, preference="prefer_lift").json()["steps"]

    for step in steps:
        words = f"{step['instruction']} {step['detail']}".lower().split()
        assert "up" not in words
        assert "down" not in words


def test_a_route_to_the_same_node_has_no_steps(client: TestClient) -> None:
    body = post_route(client, ORIGIN, ORIGIN).json()

    assert body["steps"] == []


# --------------------------------------------------------------------------
# POST /route: preferences and restrictions
# --------------------------------------------------------------------------
#
# The default route from Hive_B5_A to Hive_B4_E takes the staircase: 4 nodes,
# 36 s. Avoiding stairs forces the longer way round via the lift: 8 nodes,
# 67 s. Those two shapes are what these tests tell apart.


def post_route_with(client: TestClient, **options):
    """POST the standard route, plus whatever preference options are given."""
    payload = {"origin": ORIGIN, "destination": DESTINATION, **options}
    return client.post("/route", json=payload)


def test_defaults_match_an_explicit_fastest_request(client: TestClient) -> None:
    """Sending no options must mean the same as asking for the fastest route."""
    implicit = post_route(client, ORIGIN, DESTINATION).json()
    explicit = post_route_with(
        client,
        preference="fastest",
        allow_stairs=True,
        allow_lift=True,
        sheltered_only=False,
    ).json()

    assert implicit == explicit


def test_prefer_lift_takes_the_lift_instead_of_the_stairs(
    client: TestClient,
) -> None:
    body = post_route_with(client, preference="prefer_lift").json()

    assert body["uses_lift"] is True
    assert body["uses_stairs"] is False
    # Avoiding the stairs is a longer walk; that is the trade being made.
    assert body["total_walk_seconds"] > EXPECTED_SECONDS


def test_prefer_lift_still_reports_honest_totals(client: TestClient) -> None:
    """The stairs penalty steers the search; it must not leak into the totals.

    ``total_walk_seconds`` is summed from the edges themselves, so it stays a
    real walking time rather than the inflated score used to compare routes.
    """
    body = post_route_with(client, preference="prefer_lift").json()

    assert body["total_walk_seconds"] == pytest.approx(67.0)


def test_refusing_stairs_forces_the_lift_route(client: TestClient) -> None:
    body = post_route_with(client, allow_stairs=False).json()

    assert body["uses_stairs"] is False
    assert body["uses_lift"] is True


def test_refusing_the_lift_keeps_the_staircase_route(client: TestClient) -> None:
    body = post_route_with(client, allow_lift=False).json()

    assert body["uses_lift"] is False
    assert body["nodes"] == EXPECTED_NODES


def test_no_route_when_both_stairs_and_lift_are_refused(client: TestClient) -> None:
    """Origin and destination are on different floors, so something must give."""
    response = post_route_with(client, allow_stairs=False, allow_lift=False)

    assert response.status_code == 404


def test_the_404_says_which_restrictions_caused_it(client: TestClient) -> None:
    """'No route' alone is not actionable; the user needs to know why."""
    detail = post_route_with(
        client, allow_stairs=False, allow_lift=False
    ).json()["detail"]

    assert "no stairs" in detail
    assert "no lift" in detail


def test_refusing_both_still_works_on_a_single_floor(client: TestClient) -> None:
    """Ruling out stairs and lifts is fine when no floor change is needed."""
    response = client.post(
        "/route",
        json={
            "origin": "Hive_B5_A",
            "destination": "Hive_B5_G",
            "allow_stairs": False,
            "allow_lift": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["uses_stairs"] is False


def test_sheltered_only_succeeds_because_every_edge_is_covered(
    client: TestClient,
) -> None:
    body = post_route_with(client, sheltered_only=True).json()

    assert body["fully_sheltered"] is True
    assert body["nodes"] == EXPECTED_NODES


def test_an_unknown_preference_is_rejected(client: TestClient) -> None:
    response = post_route_with(client, preference="teleport")

    assert response.status_code == 422


# --------------------------------------------------------------------------
# POST /route: unknown node ids
# --------------------------------------------------------------------------


def test_unknown_origin_returns_404(client: TestClient) -> None:
    response = post_route(client, "Hive_B9_NOPE", DESTINATION)

    assert response.status_code == 404
    assert "Hive_B9_NOPE" in response.json()["detail"]


def test_unknown_destination_returns_404(client: TestClient) -> None:
    response = post_route(client, ORIGIN, "Hive_B9_NOPE")

    assert response.status_code == 404
    assert "Hive_B9_NOPE" in response.json()["detail"]


def test_unknown_ids_are_404_not_422(client: TestClient) -> None:
    """A well-formed request naming a missing node is 'not found', not invalid."""
    response = post_route(client, "Hive_B9_NOPE", "Hive_B9_ALSO_NOPE")

    assert response.status_code == 404


# --------------------------------------------------------------------------
# POST /route: malformed requests (FastAPI's own validation)
# --------------------------------------------------------------------------


def test_missing_destination_returns_422(client: TestClient) -> None:
    response = client.post("/route", json={"origin": ORIGIN})

    assert response.status_code == 422


def test_empty_origin_returns_422(client: TestClient) -> None:
    response = post_route(client, "", DESTINATION)

    assert response.status_code == 422


def test_unexpected_field_returns_422(client: TestClient) -> None:
    """RouteRequest forbids extra fields, so a typo is reported, not ignored."""
    response = client.post(
        "/route",
        json={"origin": ORIGIN, "destination": DESTINATION, "origins": "typo"},
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# POST /route: no route available
# --------------------------------------------------------------------------


def test_no_route_returns_404(cut_off_client: TestClient) -> None:
    """Both nodes exist, but every corridor to Hive_B5_B is blocked."""
    response = post_route(cut_off_client, ORIGIN, "Hive_B5_B")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "Hive_B5_B" in detail


def test_cut_off_graph_still_serves_reachable_routes(
    cut_off_client: TestClient,
) -> None:
    """Confirms the fixture blocks one node rather than breaking the graph."""
    response = post_route(cut_off_client, ORIGIN, DESTINATION)

    assert response.status_code == 200
    assert response.json()["nodes"] == EXPECTED_NODES


# --------------------------------------------------------------------------
# CORS: which browser pages may read our responses
# --------------------------------------------------------------------------
#
# CORS lives entirely in HTTP response headers. The server always does the work
# and always sends the answer; the *browser* then decides whether the calling
# page is allowed to read it, based on those headers. So these tests check the
# headers, not the status codes.

DISALLOWED_ORIGIN = "http://evil.example.com"


@pytest.mark.parametrize("origin", DEV_ALLOWED_ORIGINS)
def test_preflight_from_an_allowed_origin_is_accepted(
    client: TestClient, origin: str
) -> None:
    """Before a real POST, a browser sends an OPTIONS 'may I?' request."""
    response = client.options(
        "/route",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


@pytest.mark.parametrize("origin", DEV_ALLOWED_ORIGINS)
def test_allowed_origin_may_read_a_route_response(
    client: TestClient, origin: str
) -> None:
    response = post_route_with_origin(client, origin)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("origin", DEV_ALLOWED_ORIGINS)
def test_allowed_origin_may_read_the_health_response(
    client: TestClient, origin: str
) -> None:
    response = client.get("/health", headers={"Origin": origin})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_disallowed_origin_gets_no_cors_header(client: TestClient) -> None:
    """The security-relevant case: a stranger's page cannot read the answer.

    The request itself still runs, but without the allow-origin header the
    browser refuses to hand the response to the calling page.
    """
    response = post_route_with_origin(client, DISALLOWED_ORIGIN)

    assert "access-control-allow-origin" not in response.headers


def test_wildcard_origin_is_never_returned(client: TestClient) -> None:
    """Guards the 'no allow_origins=["*"]' requirement."""
    for origin in [*DEV_ALLOWED_ORIGINS, DISALLOWED_ORIGIN]:
        response = post_route_with_origin(client, origin)
        assert response.headers.get("access-control-allow-origin") != "*"


def test_only_the_methods_the_app_uses_are_offered(client: TestClient) -> None:
    """PATCH and DELETE are for the admin screen; PUT is used nowhere."""
    allowed = client.options(
        "/route",
        headers={
            "Origin": DEV_ALLOWED_ORIGINS[0],
            "Access-Control-Request-Method": "POST",
        },
    ).headers["access-control-allow-methods"]

    offered = {method.strip() for method in allowed.split(",")}
    assert offered == {"GET", "POST", "PATCH", "DELETE"}
    assert "PUT" not in offered


def test_requests_without_an_origin_are_untouched(client: TestClient) -> None:
    """curl, pytest and server-to-server callers see no CORS headers at all."""
    response = post_route(client, ORIGIN, DESTINATION)

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_does_not_change_the_route_response_body(client: TestClient) -> None:
    """Adding CORS must not alter what /route actually returns."""
    without_origin = post_route(client, ORIGIN, DESTINATION).json()
    with_origin = post_route_with_origin(client, DEV_ALLOWED_ORIGINS[0]).json()

    assert with_origin == without_origin


# --------------------------------------------------------------------------
# Safety net
# --------------------------------------------------------------------------


def test_real_graph_file_is_never_modified(graph_path: Path) -> None:
    """Guard for requirement 10: tests must not rewrite data/campus_graph.json."""
    data = json.loads(graph_path.read_text(encoding="utf-8"))

    assert len(data["nodes"]) == 17
    assert len(data["edges"]) == 27
    assert all(edge["blocked"] is False for edge in data["edges"])

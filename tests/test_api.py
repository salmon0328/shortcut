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


def test_methods_beyond_get_and_post_are_not_offered(client: TestClient) -> None:
    allowed = client.options(
        "/route",
        headers={
            "Origin": DEV_ALLOWED_ORIGINS[0],
            "Access-Control-Request-Method": "POST",
        },
    ).headers["access-control-allow-methods"]

    assert "DELETE" not in allowed
    assert "PUT" not in allowed


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

"""Tests for shuttle rides, and for offering other ways round.

The scenario every test here uses is a real trade-off, because that is the
only situation where either feature does anything interesting:

* walking straight there    - 1600 m on foot, 300 s
* walking to a stop, riding -  120 m on foot, 330 s moving plus 120 s waiting

Neither is simply better. The fast way walks you a mile; the easy way makes
you wait for a bus.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import app

START = "Hive_B5_D"  # Main Entrance
FAR_STOP = "Stop_South"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A client whose map has a shuttle route added to it."""
    with TestClient(app) as test_client:
        app.state.overrides_path = tmp_path / "graph_overrides.json"

        for stop_id, name in [("Stop_North", "North Stop"), (FAR_STOP, "South Stop")]:
            test_client.post(
                "/admin/nodes",
                json={
                    "id": stop_id,
                    "name": name,
                    "building": "Campus",
                    "floor": "G",
                    "type": "junction",
                },
            )

        # Out of the building to the nearest stop.
        test_client.post(
            "/admin/edges",
            json={
                "from_id": START,
                "to_id": "Stop_North",
                "distance_m": 120,
                "walk_seconds": 90,
            },
        )
        # The ride itself: a long way, but none of it on foot.
        test_client.post(
            "/admin/edges",
            json={
                "from_id": "Stop_North",
                "to_id": FAR_STOP,
                "distance_m": 1500,
                "walk_seconds": 240,
                "wait_seconds": 120,
                "shuttle": True,
            },
        )
        # Or walk the whole way, which is quicker but far more walking.
        test_client.post(
            "/admin/edges",
            json={
                "from_id": START,
                "to_id": FAR_STOP,
                "distance_m": 1600,
                "walk_seconds": 300,
            },
        )

        yield test_client


def route(client: TestClient, **options):
    payload = {"origin": START, "destination": FAR_STOP}
    payload.update(options)
    return client.post("/route", json=payload).json()


def options(client: TestClient, **extra):
    payload = {"origin": START, "destination": FAR_STOP}
    payload.update(extra)
    return client.post("/route/options", json=payload).json()


# --------------------------------------------------------------------------
# Shuttle rides
# --------------------------------------------------------------------------


def test_riding_does_not_count_as_walking(client: TestClient) -> None:
    """The whole point of the shuttle: distance covered without walking it."""
    body = route(client, preference="least_walking")

    assert body["uses_shuttle"] is True
    assert body["walking_distance_m"] == pytest.approx(120)
    assert body["total_distance_m"] == pytest.approx(1620)


def test_the_full_distance_is_still_reported_honestly(client: TestClient) -> None:
    """A 1.5 km ride is 1.5 km travelled, even though none of it is walked."""
    body = route(client, preference="least_walking")

    assert body["total_distance_m"] > body["walking_distance_m"]


def test_waiting_is_reported_separately_from_moving(client: TestClient) -> None:
    """Standing at a stop is not travelling, and should not read as if it is."""
    body = route(client, preference="least_walking")

    assert body["total_wait_seconds"] == pytest.approx(120)
    assert body["total_walk_seconds"] == pytest.approx(330)


def test_fastest_walks_rather_than_waits(client: TestClient) -> None:
    body = route(client, preference="fastest")

    assert body["uses_shuttle"] is False
    assert body["walking_distance_m"] == pytest.approx(1600)


def test_least_walking_rides_rather_than_walks(client: TestClient) -> None:
    body = route(client, preference="least_walking")

    assert body["uses_shuttle"] is True
    assert body["walking_distance_m"] < 200


def test_the_shuttle_can_be_refused(client: TestClient) -> None:
    body = route(client, preference="least_walking", allow_shuttle=False)

    assert body["uses_shuttle"] is False
    assert body["walking_distance_m"] == pytest.approx(1600)


def test_a_shuttle_step_is_described_as_a_ride(client: TestClient) -> None:
    steps = route(client, preference="least_walking")["steps"]

    riding = [step for step in steps if step["shuttle"]]
    assert len(riding) == 1
    assert "shuttle" in riding[0]["instruction"].lower()
    assert riding[0]["wait_seconds"] == pytest.approx(120)


def test_a_walking_step_is_not_marked_as_a_ride(client: TestClient) -> None:
    steps = route(client, preference="least_walking")["steps"]

    assert steps[0]["shuttle"] is False
    assert steps[0]["wait_seconds"] == 0


def test_refusing_everything_says_which_choice_caused_it(
    client: TestClient,
) -> None:
    response = client.post(
        "/route",
        json={
            "origin": START,
            "destination": FAR_STOP,
            "allow_shuttle": False,
            "sheltered_only": True,
        },
    )

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "no shuttle" in detail


# --------------------------------------------------------------------------
# Other ways round
# --------------------------------------------------------------------------


def test_asking_for_the_fastest_offers_a_way_with_less_walking(
    client: TestClient,
) -> None:
    """The trade the user asked for: quicker route, gentler alternative."""
    body = options(client, preference="fastest")

    assert body["primary"]["route"]["uses_shuttle"] is False
    assert len(body["alternatives"]) >= 1

    alternative = body["alternatives"][0]["route"]
    assert alternative["walking_distance_m"] < body["primary"]["route"]["walking_distance_m"]


def test_asking_to_walk_less_offers_a_quicker_way(client: TestClient) -> None:
    body = options(client, preference="least_walking")

    assert body["primary"]["route"]["uses_shuttle"] is True
    assert len(body["alternatives"]) >= 1

    alternative = body["alternatives"][0]["route"]
    assert alternative["total_walk_seconds"] < body["primary"]["route"]["total_walk_seconds"]


def test_an_alternative_explains_what_it_trades(client: TestClient) -> None:
    """"A different way round" is not enough to choose between two routes."""
    why = options(client, preference="fastest")["alternatives"][0]["why"]

    assert "less walking" in why
    assert "slower" in why


def test_alternatives_are_never_worse_at_everything(client: TestClient) -> None:
    """A route that loses on both counts is not an alternative, just worse."""
    body = options(client, preference="fastest")
    primary = body["primary"]["route"]

    for option in body["alternatives"]:
        route_body = option["route"]
        assert route_body["walking_distance_m"] < primary["walking_distance_m"]


def test_the_primary_matches_a_plain_route_request(client: TestClient) -> None:
    """Asking for options must not quietly change the route itself."""
    plain = route(client, preference="fastest")
    primary = options(client, preference="fastest")["primary"]["route"]

    assert primary["nodes"] == plain["nodes"]
    assert primary["total_walk_seconds"] == pytest.approx(plain["total_walk_seconds"])


def test_options_are_the_same_every_time(client: TestClient) -> None:
    first = options(client, preference="fastest")

    for _ in range(3):
        assert options(client, preference="fastest") == first


def test_a_route_with_only_one_way_offers_no_alternatives(
    client: TestClient,
) -> None:
    """Nothing to choose between, so nothing is invented."""
    body = client.post(
        "/route/options",
        json={"origin": "Hive_B5_A", "destination": "Hive_B5_A"},
    ).json()

    assert body["alternatives"] == []


def test_unknown_places_are_still_a_404(client: TestClient) -> None:
    response = client.post(
        "/route/options",
        json={"origin": "Nowhere", "destination": FAR_STOP},
    )

    assert response.status_code == 404

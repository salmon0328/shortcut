"""The soft shelter preference.

``sheltered_only`` was always a hard filter: no covered way, no route at all.
That is the right answer for someone who genuinely cannot get wet, and the
wrong one for the far commoner case of "it is raining and I would rather not".
``sheltered_cost`` is the soft counterpart - it prefers cover and still
answers - and these tests pin the difference.

They run on a synthetic graph, deliberately. Every corridor in the survey is
covered, and rightly so: the Hive's B4 and B5 are indoors. Pinning shelter
behaviour to whichever edge happens to be exposed would make this file a
hostage to the next survey trip - it would go red the day someone corrects a
measurement, and quietly stop testing anything the day the last uncovered
edge was fixed. What must hold is the *behaviour*: given something exposed,
prefer cover, and price the detour rather than forbidding it.

The same reasoning, and the same shape, as the stairs-and-lift graph in
``tests/test_api.py``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import app, get_graph
from shortcut.graph_store import CampusGraph, Edge, load_graph
from shortcut.tools.astar import (
    DEFAULT_EXPOSURE_PENALTY_SECONDS,
    edge_seconds,
    find_route,
    sheltered_cost,
)

# Two ways from one place to another: straight across the open, or the long
# way round under cover. The numbers matter - the dry way is exactly 5 seconds
# dearer, which is small enough that a weak preference should still take the
# quick wet route and a real one should not.
START = "Test_Dry_Start"
MIDDLE = "Test_Dry_Middle"
END = "Test_Dry_End"

WET_AND_QUICK = (START, END)
DRY_AND_SLOWER = (START, MIDDLE, END)

WET_SECONDS = 12.0
DRY_SECONDS = 17.0  # 9 + 8


def _place(node_id: str, name: str) -> dict:
    return {
        "id": node_id,
        "name": name,
        "building": "Test",
        "floor": "W",
        "type": "junction",
    }


def _link(edge_id: str, start: str, end: str, seconds: float, *, covered: bool) -> dict:
    return {
        "id": edge_id,
        "from": start,
        "to": end,
        "distance_m": round(seconds * 1.4, 1),
        "walk_seconds": seconds,
        "covered": covered,
        "stairs": False,
        "lift": False,
        "blocked": False,
    }


@pytest.fixture
def weather_graph(graph_path: Path, tmp_path: Path) -> CampusGraph:
    """The real map, plus three places joined by a wet way and a dry way."""
    data = copy.deepcopy(json.loads(graph_path.read_text(encoding="utf-8")))
    data["nodes"] += [
        _place(START, "Test Dry Start"),
        _place(MIDDLE, "Test Dry Middle"),
        _place(END, "Test Dry End"),
    ]
    data["edges"] += [
        _link("Test_Wet", START, END, WET_SECONDS, covered=False),
        _link("Test_Dry_1", START, MIDDLE, 9.0, covered=True),
        _link("Test_Dry_2", MIDDLE, END, 8.0, covered=True),
    ]
    temporary_file = tmp_path / "weather.json"
    temporary_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return load_graph(temporary_file)


@pytest.fixture
def weather_client(weather_graph: CampusGraph) -> Iterator[TestClient]:
    app.dependency_overrides[get_graph] = lambda: weather_graph
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _is_covered(graph: CampusGraph, edge_ids: tuple[str, ...]) -> bool:
    return all(graph.edge_by_id(edge_id).covered for edge_id in edge_ids)


# --------------------------------------------------------------------------
# The cost function on its own
# --------------------------------------------------------------------------


def test_a_covered_edge_costs_exactly_what_it_would_anyway() -> None:
    """No graph needed: the penalty must only touch what is exposed."""
    indoors = Edge(
        id="e", from_id="a", to_id="b", distance_m=10, walk_seconds=8, covered=True
    )

    assert sheltered_cost()(indoors) == edge_seconds(indoors)


def test_an_uncovered_edge_costs_the_penalty_more() -> None:
    outdoors = Edge(
        id="e", from_id="a", to_id="b", distance_m=10, walk_seconds=8, covered=False
    )

    assert sheltered_cost()(outdoors) == edge_seconds(outdoors) + DEFAULT_EXPOSURE_PENALTY_SECONDS


# --------------------------------------------------------------------------
# The choice it makes
# --------------------------------------------------------------------------


def test_without_a_shelter_preference_the_search_takes_the_quick_wet_way(
    weather_graph: CampusGraph,
) -> None:
    route = find_route(weather_graph, START, END, cost=edge_seconds)

    assert route.node_ids == WET_AND_QUICK
    assert not _is_covered(weather_graph, route.edge_ids)


def test_preferring_shelter_goes_round_and_stays_dry(
    weather_graph: CampusGraph,
) -> None:
    route = find_route(weather_graph, START, END, cost=sheltered_cost())

    assert route.node_ids == DRY_AND_SLOWER
    assert _is_covered(weather_graph, route.edge_ids)


def test_staying_dry_costs_real_time_and_we_can_say_how_much(
    weather_graph: CampusGraph,
) -> None:
    """The number the agent quotes in its reason has to come from somewhere."""
    quick = find_route(weather_graph, START, END, cost=edge_seconds)
    dry = find_route(weather_graph, START, END, cost=sheltered_cost())

    assert dry.total_seconds > quick.total_seconds
    assert dry.total_seconds - quick.total_seconds == pytest.approx(5.0)


def test_shelter_is_a_preference_not_a_rule(weather_graph: CampusGraph) -> None:
    """A tiny penalty is not enough to pay for the detour, so it stays wet.

    This is what separates the soft preference from ``sheltered_only``: the
    trade is priced, and sometimes the answer is that cover is not worth it.
    """
    route = find_route(
        weather_graph, START, END, cost=sheltered_cost(exposure_penalty_seconds=1.0)
    )

    assert route.node_ids == WET_AND_QUICK


def test_the_default_penalty_is_enough_to_pay_for_this_detour() -> None:
    """Guard the tuning, so lowering it cannot silently kill the demo."""
    assert DEFAULT_EXPOSURE_PENALTY_SECONDS > (DRY_SECONDS - WET_SECONDS)


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------


def test_the_api_accepts_sheltered_as_a_preference(weather_client: TestClient) -> None:
    response = weather_client.post(
        "/route",
        json={"origin": START, "destination": END, "preference": "sheltered"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["nodes"] == list(DRY_AND_SLOWER)
    assert body["fully_sheltered"] is True


def test_the_same_request_without_the_preference_gets_wet(
    weather_client: TestClient,
) -> None:
    body = weather_client.post("/route", json={"origin": START, "destination": END}).json()

    assert body["nodes"] == list(WET_AND_QUICK)
    assert body["fully_sheltered"] is False


def test_sheltered_appears_as_a_labelled_option(weather_client: TestClient) -> None:
    """Route options must label the preference, or the UI has nothing to show."""
    body = weather_client.post(
        "/route/options",
        json={"origin": START, "destination": END, "preference": "sheltered"},
    ).json()

    assert body["primary"]["label"] == "Driest"

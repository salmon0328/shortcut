"""Reading a sentence into a route request.

Almost every test here calls :func:`build_result` with a fixture intent rather
than going near a model. That is the point of splitting the parser in two: the
model's job is one call with no branches, and every branch worth testing lives
in ordinary code below it.

The one test that does exercise the model uses :class:`MockLlm`, so this file
is offline and deterministic like the rest of the suite.
"""

from __future__ import annotations

import pytest

from shortcut.ai.bedrock import MockLlm
from shortcut.ai.parser import build_result, parse_request
from shortcut.ai.prompts import PARSE_VERSION
from shortcut.ai.schemas import ParsedIntent
from shortcut.graph_store import CampusGraph
from shortcut.schemas import RouteRequest

ENTRANCE = "Hive_B5_I"
COURTYARD = "Hive_B5_G"


def intent(**overrides) -> ParsedIntent:
    """A parsed intent with both ends named, overridable field by field."""
    fields = {"origin_phrase": "main entrance", "destination_phrase": "the courtyard"}
    fields.update(overrides)
    return ParsedIntent(**fields)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_both_ends_resolve_into_a_real_route_request(graph: CampusGraph) -> None:
    result = build_result(graph, intent())

    assert isinstance(result.request, RouteRequest)
    assert result.request.origin == ENTRANCE
    assert result.request.destination == COURTYARD
    assert result.needs_clarification is False
    assert result.question is None


def test_a_stated_preference_reaches_the_request(graph: CampusGraph) -> None:
    result = build_result(graph, intent(preference="sheltered"))

    assert result.request.preference == "sheltered"


def test_a_time_budget_is_kept_beside_the_request_not_inside_it(
    graph: CampusGraph,
) -> None:
    """A* cannot price a deadline, so the agent judges against it instead."""
    result = build_result(graph, intent(max_minutes=10))

    assert result.max_minutes == 10
    assert not hasattr(result.request, "max_minutes")


# --------------------------------------------------------------------------
# Three-valued booleans: "did not say" is not "said no"
# --------------------------------------------------------------------------


def test_saying_nothing_about_stairs_leaves_the_users_toggle_alone(
    graph: CampusGraph,
) -> None:
    """The commonest bug in this design, and the reason the fields are None."""
    current = RouteRequest(origin=ENTRANCE, destination=COURTYARD, allow_stairs=False)

    result = build_result(graph, intent(avoid_stairs=None), current=current)

    assert result.request.allow_stairs is False


def test_refusing_stairs_switches_them_off(graph: CampusGraph) -> None:
    result = build_result(graph, intent(avoid_stairs=True))

    assert result.request.allow_stairs is False


def test_explicitly_accepting_stairs_switches_them_back_on(
    graph: CampusGraph,
) -> None:
    current = RouteRequest(origin=ENTRANCE, destination=COURTYARD, allow_stairs=False)

    result = build_result(graph, intent(avoid_stairs=False), current=current)

    assert result.request.allow_stairs is True


def test_an_unmentioned_preference_keeps_the_one_on_screen(
    graph: CampusGraph,
) -> None:
    current = RouteRequest(
        origin=ENTRANCE, destination=COURTYARD, preference="least_walking"
    )

    result = build_result(graph, intent(preference=None), current=current)

    assert result.request.preference == "least_walking"


# --------------------------------------------------------------------------
# Shelter: a wish and a requirement are different things
# --------------------------------------------------------------------------


def test_a_hard_shelter_requirement_switches_the_filter_on(
    graph: CampusGraph,
) -> None:
    result = build_result(graph, intent(wants_shelter=True))

    assert result.request.sheltered_only is True


def test_the_parser_never_switches_the_shelter_filter_off(
    graph: CampusGraph,
) -> None:
    """Relaxing a hard constraint is the agent's job, done in the open."""
    current = RouteRequest(origin=ENTRANCE, destination=COURTYARD, sheltered_only=True)

    result = build_result(graph, intent(wants_shelter=None), current=current)

    assert result.request.sheltered_only is True


# --------------------------------------------------------------------------
# When it cannot be sure, it asks
# --------------------------------------------------------------------------


def test_an_ambiguous_destination_asks_instead_of_guessing(
    graph: CampusGraph,
) -> None:
    result = build_result(graph, intent(destination_phrase="staircase 1"))

    assert result.request is None
    assert result.needs_clarification is True
    assert "B4" in result.question and "B5" in result.question


def test_an_ambiguous_destination_offers_exactly_two_choices(
    graph: CampusGraph,
) -> None:
    """Two is a question. Eight is a list, and nobody reads a list."""
    result = build_result(graph, intent(destination_phrase="staircase 1"))

    assert len(result.destination.alternatives) == 2


def test_an_unknown_place_is_admitted_to_rather_than_guessed_at(
    graph: CampusGraph,
) -> None:
    result = build_result(graph, intent(destination_phrase="the canteen"))

    assert result.request is None
    assert "canteen" in result.question


def test_a_missing_origin_is_asked_for(graph: CampusGraph) -> None:
    result = build_result(graph, intent(origin_phrase=None))

    assert result.request is None
    assert result.question == "Where are you starting from?"


def test_the_destination_is_asked_about_first(graph: CampusGraph) -> None:
    """One question at a time, and the vaguer half comes first."""
    result = build_result(
        graph, intent(origin_phrase="staircase 1", destination_phrase="the canteen")
    )

    assert "canteen" in result.question


# --------------------------------------------------------------------------
# Through the model
# --------------------------------------------------------------------------


def test_the_model_is_asked_once_and_its_answer_is_used(graph: CampusGraph) -> None:
    llm = MockLlm({PARSE_VERSION: intent(preference="sheltered")})

    result = parse_request(graph, llm, "main entrance to the courtyard, it's raining")

    assert result.request.preference == "sheltered"
    assert llm.calls == [(PARSE_VERSION, "ParsedIntent")]


def test_what_the_call_cost_comes_back_with_the_answer(graph: CampusGraph) -> None:
    llm = MockLlm({PARSE_VERSION: intent()})

    result = parse_request(graph, llm, "main entrance to the courtyard")

    assert result.usage.calls == 1
    assert result.usage.total_tokens > 0


def test_the_model_never_sees_or_returns_a_node_id() -> None:
    """The guarantee the whole split exists to provide."""
    fields = set(ParsedIntent.model_fields)

    assert not any("node" in field or "id" in field.split("_") for field in fields)


def test_an_invented_field_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(ValueError):
        ParsedIntent(destination_phrase="the courtyard", node_id="Hive_B5_G")

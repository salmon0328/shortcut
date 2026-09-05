"""Resolving a phrase to a place. Zero model calls in this whole file.

Place resolution is the part of the plain-language feature that must never be
left to a model, so it is ordinary code and it is tested like ordinary code.
Everything here runs against the real ``data/campus_graph.json``.
"""

from __future__ import annotations

from shortcut.ai.places import (
    AMBIGUITY_MARGIN,
    CONFIDENCE_FLOOR,
    PLACE_ALIASES,
    resolve_place,
    suggest_places,
)
from shortcut.graph_store import CampusGraph

# --------------------------------------------------------------------------
# Plain matching
# --------------------------------------------------------------------------


def test_an_exact_name_resolves(graph: CampusGraph) -> None:
    """Deliberately a name that exists on one floor only.

    Most place names in the Hive repeat on every floor - see the ambiguity
    tests below - so a name that does not is the only way to test plain
    matching without also testing the tie-break.
    """
    assert resolve_place(graph, "Pick Lockers").resolved.node_id == "Hive_B5_F"


def test_matching_ignores_case_and_surrounding_space(graph: CampusGraph) -> None:
    assert resolve_place(graph, "  cOuRtYaRd  ").resolved.node_id == "Hive_B5_G"


def test_a_node_id_resolves_to_itself(graph: CampusGraph) -> None:
    """Handy for tests and scripts, and free from the 'everything' tier."""
    assert resolve_place(graph, "Hive_B5_F").resolved.node_id == "Hive_B5_F"


def test_a_phrase_naming_nothing_here_is_refused(graph: CampusGraph) -> None:
    resolution = resolve_place(graph, "the canteen")

    assert resolution.resolved is None
    assert resolution.ambiguous is False
    assert resolution.alternatives == []


def test_an_empty_phrase_returns_nothing_rather_than_everything(
    graph: CampusGraph,
) -> None:
    assert suggest_places(graph, "   ") == []


# --------------------------------------------------------------------------
# Aliases: what students actually say
# --------------------------------------------------------------------------


def test_an_alias_resolves_to_the_surveyed_name(graph: CampusGraph) -> None:
    """Nobody says 'Pick Lockers'."""
    assert resolve_place(graph, "the lockers").resolved.node_id == "Hive_B5_F"


def test_a_lift_on_each_floor_is_asked_about_rather_than_guessed(
    graph: CampusGraph,
) -> None:
    """There is a lift on B5 and a lift on B4, and both are called "Lift".

    This is the whole reason the resolver reports ambiguity instead of
    ranking it away. Before B4 was surveyed, "lift" had one answer; the
    moment a second floor arrived it had two, and a resolver that quietly
    kept picking the first would now be wrong half the time.
    """
    resolution = resolve_place(graph, "lift")

    assert resolution.resolved is None
    assert {match.node_id for match in resolution.alternatives} == {
        "Hive_B5_B",
        "Hive_B4_B",
    }


def test_an_alias_still_separates_the_lift_from_the_lift_lobby(
    graph: CampusGraph,
) -> None:
    """"the lift" matches no place *name*, so only the alias fires.

    Which is what makes aliases worth having: they name a specific place even
    where the surveyed names collide.
    """
    assert resolve_place(graph, "the lift").resolved.node_id == "Hive_B5_B"


def test_every_alias_points_at_a_real_place(graph: CampusGraph) -> None:
    """A stale alias would resolve confidently to nothing. Catch it here."""
    missing = {
        alias: node_id
        for alias, node_id in PLACE_ALIASES.items()
        if not graph.has_node(node_id)
    }

    assert missing == {}


# --------------------------------------------------------------------------
# Typos
# --------------------------------------------------------------------------


def test_a_small_typo_still_finds_the_place(graph: CampusGraph) -> None:
    assert resolve_place(graph, "courtyad").resolved.node_id == "Hive_B5_G"


def test_a_near_miss_never_outranks_a_real_match(graph: CampusGraph) -> None:
    """Fuzzy scores are scaled below every exact tier, on purpose."""
    fuzzy = resolve_place(graph, "courtyad").resolved
    exact = resolve_place(graph, "Courtyard").resolved

    assert fuzzy.score < exact.score


# --------------------------------------------------------------------------
# Ambiguity: the part that earns its keep
# --------------------------------------------------------------------------


def test_two_places_of_the_same_name_are_not_guessed_between(
    graph: CampusGraph,
) -> None:
    """'Staircase 1' is on B5 and on B4. Picking one would be wrong half the time."""
    resolution = resolve_place(graph, "staircase 1")

    assert resolution.resolved is None
    assert resolution.ambiguous is True
    assert {match.node_id for match in resolution.alternatives} == {
        "Hive_B5_A",
        "Hive_B4_A",
    }


def test_an_ambiguous_phrase_produces_a_question_naming_both(
    graph: CampusGraph,
) -> None:
    question = resolve_place(graph, "staircase 1").question

    assert question is not None
    assert "B4" in question and "B5" in question


def test_a_resolved_phrase_asks_nothing(graph: CampusGraph) -> None:
    assert resolve_place(graph, "Courtyard").question is None


def test_the_floor_is_what_separates_the_two_staircases(graph: CampusGraph) -> None:
    """Naming the floor is enough to disambiguate, which is what the UI offers."""
    assert resolve_place(graph, "Hive_B4_A").resolved.floor == "B4"


# --------------------------------------------------------------------------
# The thresholds themselves
# --------------------------------------------------------------------------


def test_identically_named_places_fall_inside_the_ambiguity_margin(
    graph: CampusGraph,
) -> None:
    """The guard rail is the margin, so pin the arithmetic it depends on."""
    matches = suggest_places(graph, "staircase 1")

    assert matches[0].score - matches[1].score < AMBIGUITY_MARGIN


def test_the_confidence_floor_sits_above_the_weakest_tier() -> None:
    """A 'matches the building or floor' hit alone must not resolve anything."""
    assert CONFIDENCE_FLOOR > 0.40

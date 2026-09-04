"""Tests for the step wording in ``shortcut.directions``.

This module is the seam where AI-written directions will eventually replace
generated ones, so the tests here focus on two things: that generated text is
always present, and that stored text is picked up for the right direction of
travel.
"""

from __future__ import annotations

from shortcut.directions import describe_step, step_text
from shortcut.graph_store import Edge, Node


def make_node(node_id: str, name: str, floor: str = "B5", building: str = "Hive") -> Node:
    return Node(id=node_id, name=name, building=building, floor=floor, type="junction")


def make_edge(**overrides) -> Edge:
    defaults = dict(
        id="E1",
        from_id="A",
        to_id="B",
        distance_m=12.0,
        walk_seconds=9.0,
        covered=True,
    )
    defaults.update(overrides)
    return Edge(**defaults)


A = make_node("A", "Lobby")
B = make_node("B", "Courtyard")


# --------------------------------------------------------------------------
# Generated wording
# --------------------------------------------------------------------------


def test_a_plain_walk_names_both_ends_and_the_distance() -> None:
    text = describe_step(make_edge(), A, B)

    assert text.instruction == "Walk to Courtyard"
    assert "Lobby" in text.detail
    assert "12 m" in text.detail


def test_an_uncovered_walk_warns_that_it_is_not_sheltered() -> None:
    text = describe_step(make_edge(covered=False), A, B)

    assert "not sheltered" in text.detail


def test_a_covered_walk_says_nothing_about_shelter() -> None:
    text = describe_step(make_edge(covered=True), A, B)

    assert "sheltered" not in text.detail


def test_a_lift_step_talks_about_floors_not_the_node_name() -> None:
    """The node at the far end is called "Lift", so naming it would read as
    "take the lift to the lift"."""
    upstairs = make_node("B", "Lift", floor="B4")

    text = describe_step(make_edge(lift=True), A, upstairs)

    assert text.instruction == "Take the lift to Level B4"
    assert "B5" in text.detail and "B4" in text.detail
    assert "at Lift" not in text.detail


def test_a_stairs_step_talks_about_floors() -> None:
    upstairs = make_node("B", "Staircase 1", floor="B4")

    text = describe_step(make_edge(stairs=True), A, upstairs)

    assert text.instruction == "Take the stairs to Level B4"
    assert "B4" in text.detail


def test_crossing_buildings_says_so() -> None:
    elsewhere = make_node("B", "Main Entrance", building="SPMS")

    text = describe_step(make_edge(), A, elsewhere)

    assert text.instruction == "Cross to SPMS"
    assert "SPMS" in text.detail


def test_a_walk_within_one_building_does_not_mention_the_building() -> None:
    text = describe_step(make_edge(), A, B)

    assert "Hive" not in text.detail


# --------------------------------------------------------------------------
# Stored wording, the seam AI will write into
# --------------------------------------------------------------------------


def test_stored_forward_text_is_used_when_walking_forward() -> None:
    edge = make_edge(directions_forward="Head past the vending machines.")

    text = step_text(edge, A, B)

    assert text.detail == "Head past the vending machines."


def test_stored_forward_text_is_ignored_when_walking_backwards() -> None:
    """Walking B -> A must not reuse wording written for A -> B."""
    edge = make_edge(directions_forward="Head past the vending machines.")

    text = step_text(edge, B, A)

    assert text.detail != "Head past the vending machines."
    assert "Courtyard" in text.detail  # generated, starting from B


def test_stored_reverse_text_is_used_when_walking_backwards() -> None:
    edge = make_edge(directions_reverse="Double back toward the lobby.")

    text = step_text(edge, B, A)

    assert text.detail == "Double back toward the lobby."


def test_the_short_instruction_stays_generated_even_with_stored_detail() -> None:
    """Only the detail is replaceable; headings keep a predictable shape."""
    edge = make_edge(directions_forward="Head past the vending machines.")

    text = step_text(edge, A, B)

    assert text.instruction == "Walk to Courtyard"


def test_generated_text_is_used_when_nothing_is_stored() -> None:
    text = step_text(make_edge(), A, B)

    assert text.detail == describe_step(make_edge(), A, B).detail

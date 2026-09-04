"""Tests for the live overrides laid over the surveyed graph.

The point of this module is that approved reports change routing *without*
touching ``data/campus_graph.json``, so these tests care about two things:
that an override really changes the graph, and that the survey data underneath
is left alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shortcut.graph_store import CampusGraph, load_graph
from shortcut.overrides import (
    OverridesError,
    apply_overrides,
    clear_override,
    empty_overrides,
    load_overrides,
    set_override,
)

BLOCKED_EDGE = "Hive_B5_002"  # Staircase 1 <-> Courtyard


# --------------------------------------------------------------------------
# Reading and writing the overrides file
# --------------------------------------------------------------------------


def test_a_missing_file_means_no_overrides(tmp_path: Path) -> None:
    """A fresh clone has no overrides file, and that is not an error."""
    assert load_overrides(tmp_path / "nothing.json") == empty_overrides()


def test_an_override_survives_a_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "overrides.json"

    set_override(path, target_kind="edge", target_id=BLOCKED_EDGE, blocked=True)

    assert load_overrides(path)["edges"][BLOCKED_EDGE]["blocked"] is True


def test_setting_one_value_leaves_the_others_alone(tmp_path: Path) -> None:
    """Marking something crowded must not quietly un-block it."""
    path = tmp_path / "overrides.json"
    set_override(path, target_kind="edge", target_id=BLOCKED_EDGE, blocked=True)

    set_override(path, target_kind="edge", target_id=BLOCKED_EDGE, condition="flooded")

    entry = load_overrides(path)["edges"][BLOCKED_EDGE]
    assert entry["blocked"] is True
    assert entry["condition"] == "flooded"


def test_clearing_an_override_removes_it(tmp_path: Path) -> None:
    path = tmp_path / "overrides.json"
    set_override(path, target_kind="edge", target_id=BLOCKED_EDGE, blocked=True)

    clear_override(path, target_kind="edge", target_id=BLOCKED_EDGE)

    assert BLOCKED_EDGE not in load_overrides(path)["edges"]


def test_a_broken_overrides_file_is_reported_clearly(tmp_path: Path) -> None:
    path = tmp_path / "overrides.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(OverridesError) as error:
        load_overrides(path)

    assert "not valid JSON" in str(error.value)


# --------------------------------------------------------------------------
# Applying overrides to a graph
# --------------------------------------------------------------------------


def test_an_edge_override_blocks_that_edge(graph: CampusGraph) -> None:
    changed = apply_overrides(graph, {"edges": {BLOCKED_EDGE: {"blocked": True}}})

    assert changed.edge_by_id(BLOCKED_EDGE).blocked is True


def test_applying_overrides_leaves_the_original_graph_alone(
    graph: CampusGraph,
) -> None:
    """The loaded graph is shared, so it must never be edited in place."""
    apply_overrides(graph, {"edges": {BLOCKED_EDGE: {"blocked": True}}})

    assert graph.edge_by_id(BLOCKED_EDGE).blocked is False


def test_a_blocked_edge_disappears_from_routing(graph: CampusGraph) -> None:
    changed = apply_overrides(graph, {"edges": {BLOCKED_EDGE: {"blocked": True}}})

    walkable = [edge.id for edge in changed.edges_from("Hive_B5_A")]
    assert BLOCKED_EDGE not in walkable


def test_a_condition_is_recorded_without_blocking(graph: CampusGraph) -> None:
    """Crowded is a warning, not a wall."""
    changed = apply_overrides(graph, {"edges": {BLOCKED_EDGE: {"condition": "crowded"}}})

    edge = changed.edge_by_id(BLOCKED_EDGE)
    assert edge.condition == "crowded"
    assert edge.blocked is False


def test_blocking_a_node_closes_every_edge_touching_it(graph: CampusGraph) -> None:
    """There is no walking through a place that is shut."""
    changed = apply_overrides(graph, {"nodes": {"Hive_B5_G": {"blocked": True}}})

    touching = [
        edge for edge in changed.edges if "Hive_B5_G" in (edge.from_id, edge.to_id)
    ]
    assert touching, "expected the courtyard to have edges"
    assert all(edge.blocked for edge in touching)
    assert changed.neighbours("Hive_B5_G") == []


def test_a_node_condition_is_recorded_on_the_node(graph: CampusGraph) -> None:
    changed = apply_overrides(graph, {"nodes": {"Hive_B5_G": {"condition": "crowded"}}})

    assert changed.nodes["Hive_B5_G"].condition == "crowded"


def test_no_overrides_leaves_the_graph_as_surveyed(graph: CampusGraph) -> None:
    changed = apply_overrides(graph, empty_overrides())

    assert [edge.blocked for edge in changed.edges] == [
        edge.blocked for edge in graph.edges
    ]
    assert len(changed.nodes) == len(graph.nodes)


def test_the_surveyed_file_is_never_written_to(
    graph_path: Path, tmp_path: Path
) -> None:
    """Overrides go to their own file; campus_graph.json stays as surveyed."""
    before = graph_path.read_text(encoding="utf-8")

    set_override(
        tmp_path / "overrides.json",
        target_kind="edge",
        target_id=BLOCKED_EDGE,
        blocked=True,
    )
    apply_overrides(load_graph(graph_path), load_overrides(tmp_path / "overrides.json"))

    assert graph_path.read_text(encoding="utf-8") == before
    assert json.loads(before)["edges"], "sanity: the graph still has edges"

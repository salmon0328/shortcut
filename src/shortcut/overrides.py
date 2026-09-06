"""Live changes laid on top of the hand-surveyed campus graph.

``data/campus_graph.json`` is survey data: someone walked the building and
wrote it down. Two things now need to change the map while the server runs —
approved problem reports, and an administrator extending the map — and neither
should edit that file. If they did, a month of app activity would slowly
overwrite measured truth with no way to tell the two apart.

So every live change goes into one overrides file, and :func:`apply_overrides`
lays it over a freshly loaded graph at startup and after each edit. The four
sections are:

``added_nodes`` / ``added_edges``
    Whole new places and links an administrator has added.
``nodes`` / ``edges``
    Field changes to things that already exist, including the ``blocked`` and
    ``condition`` flags an approved report sets.

Deleting the overrides file returns the map to exactly what was surveyed.
Nothing here calls AI, AWS or the network.
"""

from __future__ import annotations

import json
import os
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from pathlib import Path
from typing import Any

from shortcut.graph_store import (
    CampusGraph,
    Edge,
    GraphSchemaError,
    Node,
    build_graph,
    parse_edge,
    parse_node,
)

__all__ = [
    "OverridesError",
    "NODE_PATCHABLE_FIELDS",
    "EDGE_PATCHABLE_FIELDS",
    "LIVE_CONDITION_FIELDS",
    "load_overrides",
    "save_overrides",
    "apply_overrides",
    "add_node",
    "add_edge",
    "patch_node",
    "patch_edge",
    "remove_addition",
    "remove_entity",
    "set_override",
    "clear_override",
    "empty_overrides",
]

SECTIONS = (
    "nodes",
    "edges",
    "added_nodes",
    "added_edges",
    # Surveyed places and links somebody has deleted. Recorded as a tombstone
    # rather than cut out of campus_graph.json, because the app never writes
    # the survey - that stays a deliberate act, reviewed as a git diff. So a
    # deletion behaves like every other change here: live immediately, listed
    # under pending, and folded into the survey by the graduation script.
    #
    # Keyed by id like the other sections. The value is a record about the
    # removal rather than fields to apply.
    "removed_nodes",
    "removed_edges",
)

# Only these may be changed on something that already exists. Ids are absent
# on purpose: renaming an id would orphan every edge and photo pointing at it.
NODE_PATCHABLE_FIELDS = frozenset(
    {"name", "building", "floor", "type", "x", "y", "condition", "blocked"}
)
# Fields that describe a passing state of the world rather than the building
# itself. They come from approved reports - a corridor is flooded this week,
# a lobby is crowded at lunch - and belong in the overrides file, not in the
# survey. Folding "flooded" into campus_graph.json would make this week's
# puddle a permanent feature of the map.
LIVE_CONDITION_FIELDS = frozenset({"blocked", "condition"})

EDGE_PATCHABLE_FIELDS = frozenset(
    {
        "distance_m",
        "walk_seconds",
        "covered",
        "stairs",
        "lift",
        "shuttle",
        "wait_seconds",
        "blocked",
        "one_way",
        "condition",
        "directions_forward",
        "directions_reverse",
    }
)


class OverridesError(Exception):
    """The overrides file cannot be used, or a change would break the map."""


def empty_overrides() -> dict[str, dict[str, Any]]:
    return {section: {} for section in SECTIONS}


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------


def load_overrides(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read the overrides file, or return an empty set if there is none."""
    overrides_path = Path(path)
    if not overrides_path.exists():
        return empty_overrides()

    try:
        raw = json.loads(overrides_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise OverridesError(
            f"{overrides_path} is not valid JSON: {error.msg} "
            f"(line {error.lineno}, column {error.colno})."
        ) from error
    except OSError as error:
        raise OverridesError(f"Could not read {overrides_path}: {error}") from error

    if not isinstance(raw, dict):
        raise OverridesError(
            f"{overrides_path}: expected an object, got {type(raw).__name__}."
        )

    merged = empty_overrides()
    for section in SECTIONS:
        values = raw.get(section, {})
        if not isinstance(values, dict):
            raise OverridesError(
                f"{overrides_path}: '{section}' must be an object, "
                f"got {type(values).__name__}."
            )
        merged[section] = values
    return merged


def save_overrides(path: str | Path, overrides: dict[str, dict[str, Any]]) -> None:
    """Write the overrides file, replacing it in one step."""
    overrides_path = Path(path)
    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(overrides, indent=2) + "\n"
    temporary = overrides_path.with_suffix(overrides_path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, overrides_path)


# --------------------------------------------------------------------------
# Applying them
# --------------------------------------------------------------------------


def _added_nodes(overrides: dict[str, dict[str, Any]], graph: CampusGraph) -> dict[str, Node]:
    added: dict[str, Node] = {}
    for node_id, fields in overrides.get("added_nodes", {}).items():
        if node_id in graph.nodes:
            raise OverridesError(
                f"Added node {node_id!r} already exists in the surveyed graph."
            )
        try:
            node = parse_node({**fields, "id": node_id}, f"added node {node_id!r}")
        except GraphSchemaError as error:
            raise OverridesError(str(error)) from error
        added[node_id] = node
    return added


def _added_edges(
    overrides: dict[str, dict[str, Any]], graph: CampusGraph, nodes: dict[str, Node]
) -> list[Edge]:
    added: list[Edge] = []
    for edge_id, fields in overrides.get("added_edges", {}).items():
        if edge_id in graph.edges_by_id:
            raise OverridesError(
                f"Added edge {edge_id!r} already exists in the surveyed graph."
            )
        try:
            edge = parse_edge({**fields, "id": edge_id}, f"added edge {edge_id!r}")
        except GraphSchemaError as error:
            raise OverridesError(str(error)) from error

        # An edge to nowhere would break routing, so it is refused here rather
        # than discovered later by a search that cannot find a path.
        for endpoint in (edge.from_id, edge.to_id):
            if endpoint not in nodes:
                raise OverridesError(
                    f"Added edge {edge_id!r} points at unknown node {endpoint!r}."
                )
        added.append(edge)
    return added


def _patch(entity: Any, fields: dict[str, Any], allowed: frozenset[str], what: str):
    """Apply only the permitted field changes to a node or an edge."""
    rejected = set(fields) - allowed
    if rejected:
        raise OverridesError(
            f"{what}: cannot change {', '.join(sorted(rejected))}. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )

    # Filter to fields the dataclass actually has. This is what quietly drops
    # "blocked" for a node: a place has no such field, and blocking one is
    # applied to the edges around it further down instead.
    real_fields = {field.name for field in dataclass_fields(entity)}
    changes = {
        key: value
        for key, value in fields.items()
        if key in allowed and key in real_fields
    }
    return replace(entity, **changes) if changes else entity


def apply_overrides(
    graph: CampusGraph, overrides: dict[str, dict[str, Any]]
) -> CampusGraph:
    """Return a new graph with every override applied.

    The graph passed in is never changed. Order matters: things are added
    first, so a field change or an approved report can refer to something an
    administrator added earlier.

    Raises:
        OverridesError: an addition is invalid, or a change names a field that
            may not be changed. The graph is left alone in that case.
    """
    node_patches = overrides.get("nodes", {})
    edge_patches = overrides.get("edges", {})

    # 1. New places, then new links between them.
    nodes: dict[str, Node] = {**graph.nodes, **_added_nodes(overrides, graph)}
    edges: list[Edge] = [*graph.edges, *_added_edges(overrides, graph, nodes)]

    # 2. Field changes to places.
    for node_id, fields in node_patches.items():
        node = nodes.get(node_id)
        if node is None or not isinstance(fields, dict):
            continue
        nodes[node_id] = _patch(
            node, fields, NODE_PATCHABLE_FIELDS, f"node {node_id!r}"
        )

    blocked_nodes = {
        node_id
        for node_id, fields in node_patches.items()
        if isinstance(fields, dict) and fields.get("blocked")
    }

    # 3. Field changes to links, plus the knock-on effect of a shut place.
    updated_edges: list[Edge] = []
    for edge in edges:
        fields = edge_patches.get(edge.id, {})
        if isinstance(fields, dict) and fields:
            edge = _patch(edge, fields, EDGE_PATCHABLE_FIELDS, f"edge {edge.id!r}")

        # There is no walking through a place that is shut.
        if edge.from_id in blocked_nodes or edge.to_id in blocked_nodes:
            edge = replace(edge, blocked=True)

        updated_edges.append(edge)

    # 4. Deletions, last: a place removed after being patched is still removed,
    # and doing it here means nothing above has to know about tombstones.
    gone_nodes = set(overrides.get("removed_nodes", {}))
    gone_edges = set(overrides.get("removed_edges", {}))
    if gone_nodes or gone_edges:
        nodes = {
            node_id: node for node_id, node in nodes.items() if node_id not in gone_nodes
        }
        updated_edges = [
            edge
            for edge in updated_edges
            # A link to a place that is gone cannot stay: it would point at
            # nothing, and the graph would refuse to build. Deleting a place
            # therefore deletes what led to it, which is what somebody
            # removing it means even when they have not thought it through.
            if edge.id not in gone_edges
            and edge.from_id not in gone_nodes
            and edge.to_id not in gone_nodes
        ]

    return build_graph(nodes, updated_edges, source_path=graph.source_path)


# --------------------------------------------------------------------------
# Writing changes
# --------------------------------------------------------------------------


def _update(path: str | Path, section: str, key: str, value: Any) -> dict:
    overrides = load_overrides(path)
    overrides[section][key] = value
    save_overrides(path, overrides)
    return overrides


def remove_entity(
    path: str | Path, target_kind: str, target_id: str, *, reason: str = ""
) -> dict:
    """Record that a surveyed place or link should no longer be on the map.

    A tombstone, not an edit to the survey. ``campus_graph.json`` is what
    somebody measured in the building and the app never writes it; this says
    "stop showing that" in the same live-then-graduate way as everything else,
    so the deletion is visible under pending and becomes permanent only when a
    person runs the graduation script and reads the diff.

    Links to a removed place are not recorded here. They fall away when the
    overrides are applied, so one tombstone stays correct however the map is
    edited around it.
    """
    section = "removed_nodes" if target_kind == "node" else "removed_edges"
    return _update(path, section, target_id, {"reason": reason} if reason else {})


def add_node(path: str | Path, node_id: str, fields: dict[str, Any]) -> dict:
    """Record a brand-new place."""
    return _update(path, "added_nodes", node_id, fields)


def add_edge(path: str | Path, edge_id: str, fields: dict[str, Any]) -> dict:
    """Record a brand-new link between two places."""
    return _update(path, "added_edges", edge_id, fields)


def patch_node(path: str | Path, node_id: str, fields: dict[str, Any]) -> dict:
    """Change fields on a place, whether surveyed or added.

    Changing something an administrator added is written back into its own
    entry, so the addition stays a single complete description of it.
    """
    overrides = load_overrides(path)
    if node_id in overrides["added_nodes"]:
        overrides["added_nodes"][node_id] = {
            **overrides["added_nodes"][node_id],
            **fields,
        }
    else:
        overrides["nodes"][node_id] = {**overrides["nodes"].get(node_id, {}), **fields}
    save_overrides(path, overrides)
    return overrides


def patch_edge(path: str | Path, edge_id: str, fields: dict[str, Any]) -> dict:
    """Change fields on a link, whether surveyed or added."""
    overrides = load_overrides(path)
    if edge_id in overrides["added_edges"]:
        overrides["added_edges"][edge_id] = {
            **overrides["added_edges"][edge_id],
            **fields,
        }
    else:
        overrides["edges"][edge_id] = {**overrides["edges"].get(edge_id, {}), **fields}
    save_overrides(path, overrides)
    return overrides


def remove_addition(path: str | Path, target_kind: str, target_id: str) -> bool:
    """Delete something an administrator added. Surveyed data is never removed.

    Returns whether there was an addition to remove.
    """
    overrides = load_overrides(path)
    section = "added_nodes" if target_kind == "node" else "added_edges"
    if target_id not in overrides[section]:
        return False

    del overrides[section][target_id]

    # A removed place cannot leave links dangling behind it.
    if target_kind == "node":
        orphaned = [
            edge_id
            for edge_id, fields in overrides["added_edges"].items()
            if target_id in (fields.get("from"), fields.get("to"))
        ]
        for edge_id in orphaned:
            del overrides["added_edges"][edge_id]

    save_overrides(path, overrides)
    return True


def set_override(
    path: str | Path,
    *,
    target_kind: str,
    target_id: str,
    blocked: bool | None = None,
    condition: str | None = None,
) -> dict:
    """Record a blocked or condition change, as an approved report does."""
    fields: dict[str, Any] = {}
    if blocked is not None:
        fields["blocked"] = blocked
    if condition is not None:
        fields["condition"] = condition

    if target_kind == "node":
        return patch_node(path, target_id, fields)
    return patch_edge(path, target_id, fields)


def clear_override(
    path: str | Path, *, target_kind: str, target_id: str
) -> dict:
    """Forget field changes to one thing, returning it to how it was."""
    overrides = load_overrides(path)
    section = "nodes" if target_kind == "node" else "edges"
    overrides[section].pop(target_id, None)
    save_overrides(path, overrides)
    return overrides

"""Live changes laid on top of the hand-surveyed campus graph.

``data/campus_graph.json`` is survey data: someone walked the building and
wrote it down. Approved reports must be able to close a corridor, but they
must not edit that file, or a week of user reports would slowly overwrite the
measured truth and nobody could tell which was which.

So approvals are written to a separate overrides file, and
:func:`apply_overrides` lays them over a freshly loaded graph. The source file
stays exactly as surveyed; deleting the overrides file returns the map to it.

Nothing here calls AI, AWS or the network.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from shortcut.graph_store import CampusGraph, Edge, build_graph

__all__ = [
    "OverridesError",
    "load_overrides",
    "apply_overrides",
    "set_override",
    "clear_override",
    "empty_overrides",
]


class OverridesError(Exception):
    """The overrides file exists but cannot be used."""


def empty_overrides() -> dict[str, dict[str, Any]]:
    return {"nodes": {}, "edges": {}}


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
    for section in ("nodes", "edges"):
        values = raw.get(section, {})
        if not isinstance(values, dict):
            raise OverridesError(
                f"{overrides_path}: '{section}' must be an object, "
                f"got {type(values).__name__}."
            )
        merged[section] = values
    return merged


def _write_overrides(path: Path, overrides: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(overrides, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def set_override(
    path: str | Path,
    *,
    target_kind: str,
    target_id: str,
    blocked: bool | None = None,
    condition: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Record an override for one node or edge, and save it.

    Only the values given are written, so marking something crowded does not
    quietly un-block it.
    """
    overrides_path = Path(path)
    overrides = load_overrides(overrides_path)

    section = "nodes" if target_kind == "node" else "edges"
    entry = dict(overrides[section].get(target_id, {}))
    if blocked is not None:
        entry["blocked"] = blocked
    if condition is not None:
        entry["condition"] = condition

    overrides[section][target_id] = entry
    _write_overrides(overrides_path, overrides)
    return overrides


def clear_override(
    path: str | Path, *, target_kind: str, target_id: str
) -> dict[str, dict[str, Any]]:
    """Forget any override for one node or edge, returning it to survey data."""
    overrides_path = Path(path)
    overrides = load_overrides(overrides_path)

    section = "nodes" if target_kind == "node" else "edges"
    overrides[section].pop(target_id, None)
    _write_overrides(overrides_path, overrides)
    return overrides


def apply_overrides(
    graph: CampusGraph, overrides: dict[str, dict[str, Any]]
) -> CampusGraph:
    """Return a new graph with the overrides applied.

    The graph passed in is not changed. Two rules:

    * an edge override replaces that edge's own ``blocked`` / ``condition``
    * a node override marked blocked closes every edge touching that node,
      since there is no way to walk through a place that is shut

    A node cannot be *un*-blocked into a corridor that the survey marked
    blocked; node overrides only ever take walkability away.
    """
    node_overrides = overrides.get("nodes", {})
    edge_overrides = overrides.get("edges", {})

    blocked_nodes = {
        node_id
        for node_id, entry in node_overrides.items()
        if isinstance(entry, dict) and entry.get("blocked")
    }

    updated_nodes = dict(graph.nodes)
    for node_id, entry in node_overrides.items():
        node = updated_nodes.get(node_id)
        if node is None or not isinstance(entry, dict):
            continue
        if "condition" in entry:
            updated_nodes[node_id] = replace(node, condition=entry["condition"])

    updated_edges: list[Edge] = []
    for edge in graph.edges:
        entry = edge_overrides.get(edge.id, {})
        entry = entry if isinstance(entry, dict) else {}

        blocked = bool(entry["blocked"]) if "blocked" in entry else edge.blocked
        condition = entry.get("condition", edge.condition)

        # A shut doorway closes the corridors either side of it.
        if edge.from_id in blocked_nodes or edge.to_id in blocked_nodes:
            blocked = True

        if blocked != edge.blocked or condition != edge.condition:
            edge = replace(edge, blocked=blocked, condition=condition)
        updated_edges.append(edge)

    return build_graph(updated_nodes, updated_edges, source_path=graph.source_path)

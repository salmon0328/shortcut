"""Load and query the manually created campus graph.

This module is deliberately boring: it only reads ``data/campus_graph.json``,
validates it, and offers a few small lookup helpers. No routing happens here
(see ``src/shortcut/tools/astar.py``), and no AI or network calls are involved.

Only the Python standard library is used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "GraphError",
    "GraphFileError",
    "GraphParseError",
    "GraphSchemaError",
    "UnknownEdgeError",
    "UnknownNodeError",
    "Node",
    "Edge",
    "CampusGraph",
    "build_graph",
    "load_graph",
    "get_node",
    "get_edges_from",
    "get_neighbours",
]

# A node id such as "Hive_B5_A". Used to make the type hints easier to read.
NodeId = str


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class GraphError(Exception):
    """Base class for every error raised by this module."""


class GraphFileError(GraphError):
    """The graph file is missing or could not be read."""


class GraphParseError(GraphError):
    """The graph file exists but does not contain valid JSON."""


class GraphSchemaError(GraphError):
    """The JSON parsed, but it is not shaped like a campus graph."""


class UnknownEdgeError(GraphError, KeyError):
    """An edge id was requested but does not exist."""

    def __init__(self, edge_id: str) -> None:
        message = f"Unknown edge id {edge_id!r}."
        super().__init__(message)
        self.edge_id = edge_id
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class UnknownNodeError(GraphError, KeyError):
    """A node id was requested (or referenced by an edge) but does not exist."""

    def __init__(self, node_id: str, known_ids: Iterable[str] = ()) -> None:
        known = sorted(known_ids)
        preview = ", ".join(known[:8])
        if len(known) > 8:
            preview += f", ... ({len(known)} nodes in total)"
        message = f"Unknown node id {node_id!r}."
        if preview:
            message += f" Known node ids include: {preview}"
        # KeyError repr()s its argument, so store the plain message and
        # override __str__ to keep the error readable in tracebacks.
        super().__init__(message)
        self.node_id = node_id
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """One navigation point: an entrance, junction, lift, staircase, room, ..."""

    id: NodeId
    name: str
    # Which building this point is in, e.g. "Hive". Required, because a node
    # with no building cannot be grouped or labelled once the graph covers
    # more than one building. Edges may join nodes in different buildings.
    building: str
    floor: str
    type: str
    # Optional floorplan coordinates in metres. A* can use these for its
    # heuristic later; routing must still work when they are absent.
    x: float | None = None
    y: float | None = None
    # A known problem at this place, e.g. "crowded". Advisory only; see Edge.
    condition: str | None = None
    # Anything else the JSON carried, kept so nothing is silently lost.
    extra: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Edge:
    """A walkable link between two nodes.

    Edges are two-way by default: an edge written as ``A -> B`` can also be
    walked from ``B`` to ``A``. Set ``"one_way": true`` in the JSON for the
    rare link that really is single-direction (for example an escalator).
    """

    id: str
    from_id: NodeId
    to_id: NodeId
    distance_m: float
    walk_seconds: float
    covered: bool = False
    stairs: bool = False
    lift: bool = False
    blocked: bool = False
    one_way: bool = False
    # Hand-written or (later) AI-written walking directions. Two fields
    # because an edge is two-way and the wording depends on which way you are
    # going: "forward" describes walking from_id -> to_id, "reverse" the other
    # way. Both are optional; when absent, shortcut.directions falls back to a
    # sentence generated from this edge's own numbers, so a step is never
    # left without text.
    directions_forward: str | None = None
    directions_reverse: str | None = None
    # A known problem here, e.g. "flooded" or "crowded". Advisory only: what
    # actually keeps a route away is ``blocked``. Usually set by an approved
    # report rather than written into the graph file by hand.
    condition: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def other_end(self, node_id: NodeId) -> NodeId:
        """Return the node at the far end of this edge.

        Raises:
            ValueError: if ``node_id`` is not one of this edge's endpoints.
        """
        if node_id == self.from_id:
            return self.to_id
        if node_id == self.to_id:
            return self.from_id
        raise ValueError(
            f"Node {node_id!r} is not an endpoint of edge {self.id!r} "
            f"({self.from_id!r} <-> {self.to_id!r})."
        )


@dataclass(frozen=True)
class CampusGraph:
    """The whole campus graph, plus a ready-made adjacency index."""

    nodes: dict[NodeId, Node]
    edges: list[Edge]
    # node id -> edges you can walk when standing on that node.
    adjacency: dict[NodeId, list[Edge]]
    # edge id -> that edge. Built once at load time so describing a route does
    # not rescan every edge for every step.
    edges_by_id: dict[str, Edge] = field(default_factory=dict)
    source_path: Path | None = None

    # Thin convenience wrappers so callers can use either style:
    #   get_node(graph, "Hive_B5_A")   or   graph.node("Hive_B5_A")

    def node(self, node_id: NodeId) -> Node:
        return get_node(self, node_id)

    def edges_from(self, node_id: NodeId, *, include_blocked: bool = False) -> list[Edge]:
        return get_edges_from(self, node_id, include_blocked=include_blocked)

    def neighbours(self, node_id: NodeId, *, include_blocked: bool = False) -> list[NodeId]:
        return get_neighbours(self, node_id, include_blocked=include_blocked)

    def has_node(self, node_id: NodeId) -> bool:
        return node_id in self.nodes

    def edge_by_id(self, edge_id: str) -> Edge:
        """Return the edge with ``edge_id``.

        Raises:
            UnknownEdgeError: if no such edge exists.
        """
        try:
            return self.edges_by_id[edge_id]
        except KeyError:
            raise UnknownEdgeError(edge_id) from None

    def __len__(self) -> int:
        return len(self.nodes)


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------


def _require_str(raw: dict[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GraphSchemaError(
            f"{where}: field {key!r} must be a non-empty string, got {value!r}."
        )
    return value


def _optional_str(raw: dict[str, Any], key: str, where: str, default: str = "") -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise GraphSchemaError(f"{where}: field {key!r} must be a string, got {value!r}.")
    return value


def _require_number(raw: dict[str, Any], key: str, where: str) -> float:
    value = raw.get(key)
    # bool is a subclass of int in Python, so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphSchemaError(
            f"{where}: field {key!r} must be a number, got {value!r}."
        )
    if value < 0:
        raise GraphSchemaError(f"{where}: field {key!r} must not be negative, got {value!r}.")
    return float(value)


def _optional_number(raw: dict[str, Any], key: str, where: str) -> float | None:
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphSchemaError(f"{where}: field {key!r} must be a number, got {value!r}.")
    return float(value)


def _optional_text(raw: dict[str, Any], key: str, where: str) -> str | None:
    """A free-text field that may be absent, but must be a string if present."""
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if not isinstance(value, str):
        raise GraphSchemaError(f"{where}: field {key!r} must be a string, got {value!r}.")
    stripped = value.strip()
    return stripped or None


def _optional_bool(raw: dict[str, Any], key: str, where: str, default: bool = False) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise GraphSchemaError(
            f"{where}: field {key!r} must be true or false, got {value!r}."
        )
    return value


def _parse_node(raw: Any, index: int) -> Node:
    where = f"nodes[{index}]"
    if not isinstance(raw, dict):
        raise GraphSchemaError(f"{where}: each node must be a JSON object, got {type(raw).__name__}.")

    node_id = _require_str(raw, "id", where)
    known_keys = {"id", "name", "building", "floor", "type", "x", "y", "condition"}
    return Node(
        id=node_id,
        name=_optional_str(raw, "name", where, default=node_id),
        building=_require_str(raw, "building", where),
        floor=_optional_str(raw, "floor", where),
        type=_optional_str(raw, "type", where, default="point"),
        x=_optional_number(raw, "x", where),
        y=_optional_number(raw, "y", where),
        condition=_optional_text(raw, "condition", where),
        extra={k: v for k, v in raw.items() if k not in known_keys},
    )


def _parse_edge(raw: Any, index: int) -> Edge:
    where = f"edges[{index}]"
    if not isinstance(raw, dict):
        raise GraphSchemaError(f"{where}: each edge must be a JSON object, got {type(raw).__name__}.")

    from_id = _require_str(raw, "from", where)
    to_id = _require_str(raw, "to", where)
    if from_id == to_id:
        raise GraphSchemaError(f"{where}: edge starts and ends at the same node {from_id!r}.")

    edge_id = _optional_str(raw, "id", where, default=f"{from_id}--{to_id}")
    known_keys = {
        "id", "from", "to", "distance_m", "walk_seconds",
        "covered", "stairs", "lift", "blocked", "one_way",
        "directions_forward", "directions_reverse", "condition",
    }
    return Edge(
        id=edge_id,
        from_id=from_id,
        to_id=to_id,
        distance_m=_require_number(raw, "distance_m", where),
        walk_seconds=_require_number(raw, "walk_seconds", where),
        covered=_optional_bool(raw, "covered", where),
        stairs=_optional_bool(raw, "stairs", where),
        lift=_optional_bool(raw, "lift", where),
        blocked=_optional_bool(raw, "blocked", where),
        one_way=_optional_bool(raw, "one_way", where),
        directions_forward=_optional_text(raw, "directions_forward", where),
        directions_reverse=_optional_text(raw, "directions_reverse", where),
        condition=_optional_text(raw, "condition", where),
        extra={k: v for k, v in raw.items() if k not in known_keys},
    )


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_graph(path: str | Path) -> CampusGraph:
    """Read, validate and index the campus graph stored at ``path``.

    Args:
        path: Path to a JSON file with top-level ``"nodes"`` and ``"edges"`` lists.
            Accepts a ``str`` or a ``pathlib.Path``.

    Returns:
        A :class:`CampusGraph` with every node indexed by id and an adjacency
        list that already contains both directions of each two-way edge.

    Raises:
        GraphFileError: the file is missing, is a directory, or cannot be read.
        GraphParseError: the file is not valid JSON.
        GraphSchemaError: required fields are missing, wrongly typed, or duplicated.
        UnknownNodeError: an edge references a node id that is not in ``"nodes"``.
    """
    graph_path = Path(path)

    try:
        text = graph_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise GraphFileError(
            f"Campus graph file not found: {graph_path}. "
            f"Expected something like 'data/campus_graph.json'."
        ) from error
    except IsADirectoryError as error:
        raise GraphFileError(f"Expected a JSON file but {graph_path} is a directory.") from error
    except OSError as error:
        raise GraphFileError(f"Could not read campus graph file {graph_path}: {error}") from error

    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise GraphParseError(
            f"{graph_path} is not valid JSON: {error.msg} "
            f"(line {error.lineno}, column {error.colno})."
        ) from error

    if not isinstance(data, dict):
        raise GraphSchemaError(
            f"{graph_path}: the top level of the file must be a JSON object "
            f'with "nodes" and "edges", got {type(data).__name__}.'
        )

    for key in ("nodes", "edges"):
        if key not in data:
            raise GraphSchemaError(f'{graph_path}: missing required top-level field "{key}".')
        if not isinstance(data[key], list):
            raise GraphSchemaError(
                f'{graph_path}: top-level field "{key}" must be a list, '
                f"got {type(data[key]).__name__}."
            )

    if not data["nodes"]:
        raise GraphSchemaError(f'{graph_path}: "nodes" is empty, so no route can ever be found.')

    # --- nodes ---
    nodes: dict[NodeId, Node] = {}
    for index, raw_node in enumerate(data["nodes"]):
        node = _parse_node(raw_node, index)
        if node.id in nodes:
            raise GraphSchemaError(
                f"nodes[{index}]: duplicate node id {node.id!r}. Node ids must be unique."
            )
        nodes[node.id] = node

    # --- edges ---
    edges: list[Edge] = []
    seen_edge_ids: set[str] = set()

    for index, raw_edge in enumerate(data["edges"]):
        edge = _parse_edge(raw_edge, index)

        for endpoint in (edge.from_id, edge.to_id):
            if endpoint not in nodes:
                raise UnknownNodeError(endpoint, nodes)

        if edge.id in seen_edge_ids:
            raise GraphSchemaError(
                f"edges[{index}]: duplicate edge id {edge.id!r}. Edge ids must be unique."
            )
        seen_edge_ids.add(edge.id)

        edges.append(edge)

    return build_graph(nodes, edges, source_path=graph_path)


def build_graph(
    nodes: dict[NodeId, Node],
    edges: list[Edge],
    source_path: Path | None = None,
) -> CampusGraph:
    """Assemble already-validated nodes and edges into a graph.

    Separate from :func:`load_graph` because the graph is built twice: once
    from the JSON file, and again whenever live overrides (such as an approved
    blockage report) change an edge. Both paths need the same indexes, and
    building them in one place keeps the two identical.
    """
    adjacency: dict[NodeId, list[Edge]] = {node_id: [] for node_id in nodes}

    for edge in edges:
        # Index the edge under both endpoints so it can be walked in either
        # direction; one-way edges are only reachable from their "from" node.
        adjacency[edge.from_id].append(edge)
        if not edge.one_way:
            adjacency[edge.to_id].append(edge)

    return CampusGraph(
        nodes=nodes,
        edges=edges,
        adjacency=adjacency,
        edges_by_id={edge.id: edge for edge in edges},
        source_path=source_path,
    )


# --------------------------------------------------------------------------
# Lookup helpers
# --------------------------------------------------------------------------


def get_node(graph: CampusGraph, node_id: NodeId) -> Node:
    """Return the node with ``node_id``.

    Raises:
        UnknownNodeError: if no such node exists.
    """
    try:
        return graph.nodes[node_id]
    except KeyError:
        raise UnknownNodeError(node_id, graph.nodes) from None


def get_edges_from(
    graph: CampusGraph,
    node_id: NodeId,
    *,
    include_blocked: bool = False,
) -> list[Edge]:
    """Return every edge you can walk when standing on ``node_id``.

    Two-way edges are returned no matter which end you start from, so use
    :meth:`Edge.other_end` to find where an edge leads.

    Args:
        graph: A loaded campus graph.
        node_id: Where you are standing.
        include_blocked: Set to ``True`` to also return edges marked
            ``"blocked": true`` (useful for reporting, not for routing).

    Raises:
        UnknownNodeError: if no such node exists.
    """
    if node_id not in graph.nodes:
        raise UnknownNodeError(node_id, graph.nodes)
    edges = graph.adjacency.get(node_id, [])
    if include_blocked:
        return list(edges)
    return [edge for edge in edges if not edge.blocked]


def get_neighbours(
    graph: CampusGraph,
    node_id: NodeId,
    *,
    include_blocked: bool = False,
) -> list[NodeId]:
    """Return the ids of the nodes directly reachable from ``node_id``.

    Duplicates are removed while keeping the order the edges were declared in,
    which keeps routing results deterministic.

    Raises:
        UnknownNodeError: if no such node exists.
    """
    neighbours: list[NodeId] = []
    seen: set[NodeId] = set()
    for edge in get_edges_from(graph, node_id, include_blocked=include_blocked):
        other = edge.other_end(node_id)
        if other not in seen:
            seen.add(other)
            neighbours.append(other)
    return neighbours

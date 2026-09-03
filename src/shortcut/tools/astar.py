"""Deterministic shortest-route search over the campus graph.

This is plain Python: standard library only, no Claude, no Bedrock, no
LangGraph, no network access. Given the same graph and the same origin and
destination it always returns exactly the same route.

The search is A* with a pluggable heuristic. When the nodes carry ``x``/``y``
floorplan coordinates the heuristic is a straight-line lower bound on the
remaining walking time; when they do not (which is the case today) the
heuristic is zero and A* behaves exactly like Dijkstra's algorithm. See the
module-level note in ``heuristic_for`` for why that is the right default.

The graph is only ever read, never modified.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Callable

from shortcut.graph_store import CampusGraph, Edge, NodeId, UnknownNodeError

__all__ = [
    "RoutingError",
    "NoRouteFoundError",
    "Route",
    "CostFunction",
    "Heuristic",
    "edge_seconds",
    "edge_metres",
    "zero_heuristic",
    "heuristic_for",
    "find_route",
    "find_route_or_none",
]


# A cost function scores one edge. Lower is better. Must never be negative.
CostFunction = Callable[[Edge], float]

# A heuristic estimates the remaining cost from a node to the destination.
# It must never overestimate, or the route found may not be the cheapest.
Heuristic = Callable[[NodeId], float]

# A filter decides whether an edge may be used at all (blocked edges are
# always excluded, separately from this).
EdgeFilter = Callable[[Edge], bool]


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class RoutingError(Exception):
    """Base class for routing problems."""


class NoRouteFoundError(RoutingError):
    """The origin and destination exist, but nothing connects them."""

    def __init__(self, origin: NodeId, destination: NodeId) -> None:
        super().__init__(
            f"No walkable route from {origin!r} to {destination!r}. "
            f"The nodes may be on disconnected parts of the graph, or every "
            f"connecting edge is blocked."
        )
        self.origin = origin
        self.destination = destination


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One complete answer: which nodes to walk through, and what it costs."""

    node_ids: tuple[NodeId, ...]
    edge_ids: tuple[str, ...]
    total_seconds: float
    total_distance_m: float
    uses_stairs: bool = False
    uses_lift: bool = False
    # How much of the search space A* had to open. Useful in tests and for
    # comparing heuristics; it never affects the route itself.
    nodes_expanded: int = 0

    @property
    def origin(self) -> NodeId:
        return self.node_ids[0]

    @property
    def destination(self) -> NodeId:
        return self.node_ids[-1]

    @property
    def step_count(self) -> int:
        """Number of edges walked. Zero when origin == destination."""
        return len(self.edge_ids)

    def describe(self) -> str:
        """A one-line human-readable summary, handy for printing or logging."""
        path = " -> ".join(self.node_ids)
        return (
            f"{path} "
            f"({self.total_distance_m:.1f} m, {self.total_seconds:.0f} s, "
            f"{self.step_count} steps)"
        )


# --------------------------------------------------------------------------
# Cost functions
# --------------------------------------------------------------------------


def edge_seconds(edge: Edge) -> float:
    """Default cost: estimated walking time in seconds."""
    return edge.walk_seconds


def edge_metres(edge: Edge) -> float:
    """Alternative cost: distance in metres."""
    return edge.distance_m


# --------------------------------------------------------------------------
# Heuristics
# --------------------------------------------------------------------------


def zero_heuristic(node_id: NodeId) -> float:
    """Estimate nothing. Makes A* behave exactly like Dijkstra's algorithm."""
    return 0.0


def _fastest_speed_m_per_s(graph: CampusGraph, default: float = 1.4) -> float:
    """Fastest metres-per-second seen on any usable edge in the graph.

    Dividing a straight-line distance by the *fastest* speed anywhere in the
    building can only ever under-estimate the real walking time, which is
    exactly what an admissible A* heuristic needs.
    """
    speeds = [
        edge.distance_m / edge.walk_seconds
        for edge in graph.edges
        if edge.walk_seconds > 0 and edge.distance_m > 0
    ]
    return max(speeds) if speeds else default


def heuristic_for(
    graph: CampusGraph,
    destination: NodeId,
    cost: CostFunction = edge_seconds,
) -> Heuristic:
    """Build the best admissible heuristic this graph supports.

    If the destination and the nodes being explored have ``x``/``y``
    coordinates, the estimate is the straight-line distance divided by the
    fastest walking speed in the graph. Otherwise the estimate is zero.

    Zero is always safe (never overestimates), it just means A* explores more
    of the graph than it strictly needs to. The route returned is identical
    either way, so an empty ``x``/``y`` in the JSON costs accuracy nowhere.
    """
    if cost is not edge_seconds:
        # The straight-line estimate is expressed in seconds. Mixing it with a
        # different cost unit (metres, "prefer covered" penalties, ...) could
        # overestimate and break optimality, so fall back to the safe default.
        return zero_heuristic

    goal = graph.nodes[destination]
    if goal.x is None or goal.y is None:
        return zero_heuristic

    speed = _fastest_speed_m_per_s(graph)
    goal_x, goal_y = goal.x, goal.y

    def estimate(node_id: NodeId) -> float:
        node = graph.nodes[node_id]
        if node.x is None or node.y is None:
            return 0.0
        straight_line_m = math.hypot(node.x - goal_x, node.y - goal_y)
        return straight_line_m / speed

    return estimate


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def _usable_edges(
    graph: CampusGraph,
    node_id: NodeId,
    edge_filter: EdgeFilter | None,
) -> list[Edge]:
    """Edges walkable from ``node_id``: never blocked, and passing the filter."""
    edges = graph.edges_from(node_id)  # already excludes blocked edges
    if edge_filter is None:
        return edges
    return [edge for edge in edges if edge_filter(edge)]


def _reconstruct(
    came_from: dict[NodeId, tuple[NodeId, Edge]],
    destination: NodeId,
) -> tuple[list[NodeId], list[Edge]]:
    """Walk the ``came_from`` breadcrumbs backwards, then flip them forwards."""
    node_ids: list[NodeId] = [destination]
    edges: list[Edge] = []

    current = destination
    while current in came_from:
        previous, edge = came_from[current]
        node_ids.append(previous)
        edges.append(edge)
        current = previous

    node_ids.reverse()
    edges.reverse()
    return node_ids, edges


def _build_route(
    node_ids: list[NodeId],
    edges: list[Edge],
    nodes_expanded: int,
) -> Route:
    """Add up the totals along a finished path."""
    return Route(
        node_ids=tuple(node_ids),
        edge_ids=tuple(edge.id for edge in edges),
        total_seconds=sum(edge.walk_seconds for edge in edges),
        total_distance_m=sum(edge.distance_m for edge in edges),
        uses_stairs=any(edge.stairs for edge in edges),
        uses_lift=any(edge.lift for edge in edges),
        nodes_expanded=nodes_expanded,
    )


def find_route(
    graph: CampusGraph,
    origin: NodeId,
    destination: NodeId,
    *,
    cost: CostFunction = edge_seconds,
    heuristic: Heuristic | None = None,
    edge_filter: EdgeFilter | None = None,
) -> Route:
    """Find the cheapest walkable route from ``origin`` to ``destination``.

    Args:
        graph: A graph loaded by :func:`shortcut.graph_store.load_graph`.
            It is only read, never modified.
        origin: Node id to start from.
        destination: Node id to finish at.
        cost: Scores one edge; lower is better. Defaults to walking seconds.
        heuristic: Remaining-cost estimate. Defaults to the best admissible
            heuristic the graph supports (see :func:`heuristic_for`).
        edge_filter: Optional extra rule, e.g. ``lambda e: not e.stairs`` for a
            step-free route. Blocked edges are always excluded regardless.

    Returns:
        A :class:`Route` with the ordered node ids, ordered edge ids, total
        walking time in seconds and total distance in metres.

    Raises:
        UnknownNodeError: ``origin`` or ``destination`` is not in the graph.
        NoRouteFoundError: both nodes exist but nothing usable connects them.
        ValueError: the cost function returned a negative number.
    """
    if origin not in graph.nodes:
        raise UnknownNodeError(origin, graph.nodes)
    if destination not in graph.nodes:
        raise UnknownNodeError(destination, graph.nodes)

    if origin == destination:
        return _build_route([origin], [], nodes_expanded=0)

    if heuristic is None:
        heuristic = heuristic_for(graph, destination, cost)

    # best_cost[node] = cheapest total cost found so far to reach that node.
    best_cost: dict[NodeId, float] = {origin: 0.0}
    # came_from[node] = (previous node, edge walked to get here).
    came_from: dict[NodeId, tuple[NodeId, Edge]] = {}
    settled: set[NodeId] = set()
    nodes_expanded = 0

    # Priority queue of (estimated total cost, cost so far, node id).
    # Ties are broken first by the lower cost-so-far, then alphabetically by
    # node id, so the result never depends on dict or heap ordering.
    queue: list[tuple[float, float, NodeId]] = [(heuristic(origin), 0.0, origin)]

    while queue:
        _estimated_total, cost_so_far, current = heapq.heappop(queue)

        if current in settled:
            # A cheaper way to this node was already processed. Stale entry.
            continue
        settled.add(current)
        nodes_expanded += 1

        if current == destination:
            node_ids, edges = _reconstruct(came_from, destination)
            return _build_route(node_ids, edges, nodes_expanded)

        for edge in _usable_edges(graph, current, edge_filter):
            neighbour = edge.other_end(current)
            if neighbour in settled:
                continue

            step_cost = cost(edge)
            if step_cost < 0:
                raise ValueError(
                    f"Edge {edge.id!r} produced a negative cost ({step_cost}). "
                    f"A* requires non-negative edge costs."
                )

            new_cost = cost_so_far + step_cost
            if new_cost < best_cost.get(neighbour, math.inf):
                best_cost[neighbour] = new_cost
                came_from[neighbour] = (current, edge)
                heapq.heappush(
                    queue,
                    (new_cost + heuristic(neighbour), new_cost, neighbour),
                )

    raise NoRouteFoundError(origin, destination)


def find_route_or_none(
    graph: CampusGraph,
    origin: NodeId,
    destination: NodeId,
    **kwargs: object,
) -> Route | None:
    """Like :func:`find_route`, but returns ``None`` instead of raising.

    Useful for callers that want a safe result rather than an exception, such
    as ranking several candidate destinations at once.
    """
    try:
        return find_route(graph, origin, destination, **kwargs)  # type: ignore[arg-type]
    except (UnknownNodeError, NoRouteFoundError):
        return None

"""What approving a report would actually do to the map.

This is a tool, not an agent and not a model call: given a place and a
condition, it works out what closing that place would cost, by arithmetic on
the graph. The Verifier calls it, reads the answer, and raises its own bar
when the answer is bad enough - which is the whole reason it exists. Deciding
whether to believe a report on the strength of the report alone means treating
"the lift on B4 is busy" and "the only walkway between two buildings is shut"
as the same size of claim, and they are not.

Two questions, both cheap:

**What gets cut off.** A closed corridor that leaves places with no way in at
all is not a routing inconvenience, it is a hole in the map, and no amount of
corroboration should let one land without a person seeing it first.

**How much longer the way round is.** A closure with a parallel corridor beside
it costs seconds; one that sends people round the outside of a building costs
minutes. The number is the difference between the two, in seconds, and it is
what turns "this closes something" into "this closes something that matters".

Nothing here writes anything. The graph is read, a copy of its adjacency is
walked, and the real map is left exactly as it was - the caller is deciding
*whether* to approve, and a tool that applied the change to find out would
have already done the thing it was asked about.
"""

from __future__ import annotations

from dataclasses import dataclass

from shortcut.graph_store import CampusGraph, NodeId
from shortcut.report_store import ROUTE_BLOCKING_CONDITIONS
from shortcut.tools.astar import find_route_or_none

__all__ = ["Impact", "assess"]


@dataclass(frozen=True)
class Impact:
    """What closing one place or link would do to the rest of the map."""

    #: Whether this condition closes anything at all. "crowded" does not: it
    #: warns, and a warning cannot strand anybody.
    blocks_routes: bool
    #: Places that lose every way in, not counting the reported place itself -
    #: that one is closed on purpose, and listing it would bury the collateral
    #: among the intended.
    cut_off: tuple[NodeId, ...] = ()
    #: How many more seconds the way round takes, for a closed link whose ends
    #: both survive. ``None`` when there is nothing to compare: an unblocking
    #: condition, a closed place rather than a link, or no way round at all.
    detour_seconds: float | None = None

    @property
    def isolates(self) -> bool:
        return bool(self.cut_off)

    @property
    def describes(self) -> str:
        """One line for a prompt, a log or a reviewer."""
        if not self.blocks_routes:
            return "Nothing closes: this condition only puts a warning on the map."
        if self.cut_off:
            return (
                f"Closing this cuts off {len(self.cut_off)} place(s) with no way "
                f"in at all: {', '.join(self.cut_off)}."
            )
        if self.detour_seconds is None:
            return "Closing this leaves every place reachable."
        return (
            f"Closing this leaves every place reachable; the way round costs "
            f"about {round(self.detour_seconds)}s more."
        )


def _closed_edge_ids(
    graph: CampusGraph, target_kind: str, target_id: str
) -> set[str]:
    """Which edges stop being walkable if this target is closed.

    Closing a place closes every link that touches it, which is exactly what
    ``apply_overrides`` does when it writes a blocked node - worked out the
    same way here so the estimate and the real thing cannot disagree.
    """
    if target_kind == "edge":
        return {target_id}
    return {
        edge.id
        for edge in graph.edges
        if edge.from_id == target_id or edge.to_id == target_id
    }


def _component_of(
    graph: CampusGraph, start: NodeId, without: set[str]
) -> set[NodeId]:
    """Every place reachable from ``start``, ignoring the closed edges."""
    seen: set[NodeId] = {start}
    queue: list[NodeId] = [start]
    while queue:
        node_id = queue.pop()
        for edge in graph.adjacency.get(node_id, []):
            if edge.blocked or edge.id in without:
                continue
            beyond = edge.from_id if edge.to_id == node_id else edge.to_id
            if beyond not in seen and beyond in graph.nodes:
                seen.add(beyond)
                queue.append(beyond)
    return seen


def _largest_component(graph: CampusGraph, without: set[str]) -> set[NodeId]:
    """The biggest piece the map falls into once those edges are gone.

    "The map" has to mean something to say what fell off it, and the biggest
    remaining piece is the only defensible reading: it is where nearly
    everybody is, and it does not need a hardcoded anchor node that a resurvey
    could rename out from under this.
    """
    unvisited = set(graph.nodes)
    biggest: set[NodeId] = set()
    while unvisited:
        # Sorted, so a graph that splits into two equal halves picks the same
        # one every run and the answer never depends on dict ordering.
        component = _component_of(graph, min(unvisited), without)
        if len(component) > len(biggest):
            biggest = component
        unvisited -= component
    return biggest


def assess(
    graph: CampusGraph, *, target_kind: str, target_id: str, condition: str
) -> Impact:
    """What approving this report would do, without approving it."""
    if condition not in ROUTE_BLOCKING_CONDITIONS:
        return Impact(blocks_routes=False)

    closed = _closed_edge_ids(graph, target_kind, target_id)
    if not closed:
        return Impact(blocks_routes=True)

    before = _largest_component(graph, set())
    after = _largest_component(graph, closed)

    # The reported place is meant to close, so it is not collateral. Every
    # other place that was on the map and no longer is, is.
    excluded = {target_id} if target_kind == "node" else set()
    cut_off = tuple(sorted((before - after) - excluded))

    return Impact(
        blocks_routes=True,
        cut_off=cut_off,
        detour_seconds=_detour(graph, target_kind, target_id, closed, after),
    )


def _detour(
    graph: CampusGraph,
    target_kind: str,
    target_id: str,
    closed: set[str],
    survives: set[NodeId],
) -> float | None:
    """How much longer the way round a closed link is, in seconds.

    Only asked of a link. A closed *place* has no "way round" to price - the
    place itself is the destination somebody wanted - so the honest answer
    there is no number rather than one measured between its neighbours.
    """
    if target_kind != "edge":
        return None

    edge = graph.edges_by_id.get(target_id)
    if edge is None or edge.from_id not in survives or edge.to_id not in survives:
        return None

    direct = find_route_or_none(graph, edge.from_id, edge.to_id)
    around = find_route_or_none(
        graph, edge.from_id, edge.to_id, edge_filter=lambda e: e.id not in closed
    )
    if direct is None or around is None:
        return None
    return round(around.total_seconds - direct.total_seconds, 1)

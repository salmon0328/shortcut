"""Request and response shapes for the Shortcut HTTP API.

These Pydantic models describe what the API accepts and what it sends back.
They are the *wire format*: the plain JSON a browser or phone app sees. The
routing itself still happens in ``shortcut.tools.astar``, which knows nothing
about HTTP.

The field names here deliberately differ from the internal ``Route`` dataclass
(``nodes`` instead of ``node_ids``, ``total_walk_seconds`` instead of
``total_seconds``), because an API is a public contract and should read
clearly on its own. :meth:`RouteResponse.from_route` is the single place that
translates between the two.

No AI, no Bedrock, no AWS: this layer only validates data.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shortcut.directions import step_text
from shortcut.graph_store import CampusGraph, Edge, Node
from shortcut.report_store import (
    CONDITIONS,
    Condition,
    Report,
    ReportGroup,
    TargetKind,
)
from shortcut.tools.astar import Route

__all__ = [
    "NodeSummary",
    "EdgeSummary",
    "RoutePreference",
    "RouteRequest",
    "RouteStep",
    "RouteResponse",
    "ReportRequest",
    "ReportSummary",
    "ReportGroupSummary",
    "ReviewResult",
]


# How a caller wants a route scored. Anything outside this set is rejected by
# Pydantic with a 422 naming the allowed values.
RoutePreference = Literal["fastest", "prefer_lift"]


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


class NodeSummary(BaseModel):
    """One navigation point, as shown in a dropdown or a picker.

    This is the API's answer to "what places exist?" It is deliberately a
    smaller view of the internal :class:`~shortcut.graph_store.Node` than the
    graph file stores: a frontend choosing where to send someone does not need
    the node's floorplan coordinates or type, just something to show and an id
    to send back in a :class:`RouteRequest`.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "id": "Hive_B5_A",
                "name": "Staircase 1",
                "building": "Hive",
                "floor": "B5",
            }
        },
    )

    id: str = Field(description="Node id to use as an origin or destination.")
    name: str = Field(
        description=(
            "Human-readable name to display, e.g. 'Staircase 1'. Not unique "
            "on its own: pair it with the building and floor to identify a "
            "place to a user."
        )
    )
    building: str = Field(description="Which building this node is in, e.g. 'Hive'.")
    floor: str = Field(description="Which floor this node is on, e.g. 'B5'.")
    condition: str | None = Field(
        default=None,
        description="A known problem here, e.g. 'crowded', from an approved report.",
    )

    @classmethod
    def from_node(cls, node: Node) -> "NodeSummary":
        """Convert a graph :class:`~shortcut.graph_store.Node` into this shape."""
        return cls(
            id=node.id,
            name=node.name,
            building=node.building,
            floor=node.floor,
            condition=node.condition,
        )


class EdgeSummary(BaseModel):
    """One corridor, stair or lift, for picking when reporting a problem.

    A route response already describes the edges it uses; this is the list to
    choose from when nobody is mid-route, such as reporting a corridor you
    walked past.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "id": "Hive_B5_002",
                "from_id": "Hive_B5_A",
                "to_id": "Hive_B5_G",
                "label": "Staircase 1 → Courtyard",
                "blocked": False,
                "condition": None,
            }
        },
    )

    id: str = Field(description="Edge id, used as a report's target_id.")
    from_id: str = Field(description="Node id at one end.")
    to_id: str = Field(description="Node id at the other end.")
    label: str = Field(description="Readable name for both ends, for display.")
    blocked: bool = Field(default=False, description="True if currently closed.")
    condition: str | None = Field(
        default=None, description="A known problem here, from an approved report."
    )

    @classmethod
    def from_edge(cls, edge: Edge, label: str) -> "EdgeSummary":
        return cls(
            id=edge.id,
            from_id=edge.from_id,
            to_id=edge.to_id,
            label=label,
            blocked=edge.blocked,
            condition=edge.condition,
        )


# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------


class RouteRequest(BaseModel):
    """What a caller must send to ask for a route.

    Both ids must be node ids that exist in ``data/campus_graph.json``, for
    example ``"Hive_B5_A"``. Whether they actually exist is checked by the
    routing code, not here: this model only checks the *shape* of the request.
    """

    # str_strip_whitespace: turn "  Hive_B5_A  " into "Hive_B5_A" before
    #   validating, so a stray space in a URL or form does not cause a 404.
    # extra="forbid": reject unexpected fields (a typo like "origins" becomes a
    #   clear validation error instead of being silently ignored).
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "origin": "Hive_B5_A",
                "destination": "Hive_B4_C",
                "preference": "fastest",
                "allow_stairs": True,
                "allow_lift": True,
                "sheltered_only": False,
            }
        },
    )

    origin: str = Field(
        min_length=1,
        max_length=100,
        description="Node id to start from, e.g. 'Hive_B5_A'.",
    )
    destination: str = Field(
        min_length=1,
        max_length=100,
        description="Node id to finish at, e.g. 'Hive_B4_C'.",
    )

    preference: RoutePreference = Field(
        default="fastest",
        description=(
            "How to score a route. 'fastest' minimises walking time. "
            "'prefer_lift' also minimises time but heavily penalises stairs, "
            "so a lift is chosen wherever one exists. This is a soft "
            "preference: stairs are still used if there is no other way."
        ),
    )
    allow_stairs: bool = Field(
        default=True,
        description="Set false to refuse any route that climbs stairs.",
    )
    allow_lift: bool = Field(
        default=True,
        description="Set false to refuse any route that uses a lift.",
    )
    sheltered_only: bool = Field(
        default=False,
        description="Set true to use only covered corridors.",
    )

    # Note: origin == destination is deliberately allowed. The router handles
    # it and returns a valid empty route, so it is not an error.
    #
    # Note: allow_stairs=false together with allow_lift=false is also allowed.
    # It is a sensible request on one floor, and simply finds no route when
    # the two nodes are on different floors.


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------


class RouteStep(BaseModel):
    """One leg of a route: walk this edge, from one node to the next.

    Steps are what a turn-by-turn screen shows one at a time. Each carries its
    own text and its own numbers, so the screen never has to add anything up
    or cross-reference another response.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "step": 1,
                "edge_id": "Hive_B5_002",
                "from_id": "Hive_B5_A",
                "to_id": "Hive_B5_G",
                "instruction": "Walk to Courtyard",
                "detail": "From Staircase 1, walk about 29 m to Courtyard.",
                "distance_m": 29.4,
                "walk_seconds": 21.0,
                "stairs": False,
                "lift": False,
                "covered": True,
            }
        },
    )

    step: int = Field(ge=1, description="Position in the route, starting at 1.")
    edge_id: str = Field(description="Id of the edge being walked.")
    from_id: str = Field(description="Node id this step starts at.")
    to_id: str = Field(description="Node id this step ends at.")
    instruction: str = Field(
        description="Short heading for the step, e.g. 'Take the lift to Level B4'."
    )
    detail: str = Field(description="Longer description of what to do.")
    distance_m: float = Field(ge=0, description="Distance walked in this step.")
    walk_seconds: float = Field(ge=0, description="Time this step takes.")
    stairs: bool = Field(default=False, description="True if this step uses stairs.")
    lift: bool = Field(default=False, description="True if this step uses a lift.")
    covered: bool = Field(default=False, description="True if this step is sheltered.")
    condition: str | None = Field(
        default=None,
        description=(
            "A known problem on this stretch, e.g. 'crowded', from an approved "
            "report. Advisory: anything that actually blocks the way is routed "
            "around instead of reported here."
        ),
    )


class RouteResponse(BaseModel):
    """One successful route, ready to send back as JSON.

    ``nodes`` and ``edges`` line up: ``edges[0]`` is the corridor walked from
    ``nodes[0]`` to ``nodes[1]``, and so on. A route therefore always has
    exactly one fewer edge than it has nodes.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "nodes": ["Hive_B5_A", "Hive_B5_G", "Hive_B5_C", "Hive_B4_C"],
                "edges": ["Hive_B5_002", "Hive_B5_007", "Hive_B5_019"],
                "total_distance_m": 55.6,
                "total_walk_seconds": 49.0,
                "uses_stairs": True,
                "uses_lift": False,
                "fully_sheltered": True,
            }
        },
    )

    nodes: list[str] = Field(
        min_length=1,
        description=(
            "Node ids to walk through, in order, starting at the origin and "
            "ending at the destination."
        ),
    )
    edges: list[str] = Field(
        default_factory=list,
        description=(
            "Edge ids walked, in order. Empty when the origin and the "
            "destination are the same node."
        ),
    )
    steps: list[RouteStep] = Field(
        default_factory=list,
        description=(
            "The same journey as 'edges', but with the text and numbers a "
            "turn-by-turn screen needs. One entry per edge, in order."
        ),
    )
    total_distance_m: float = Field(
        ge=0,
        description="Total walking distance in metres.",
    )
    total_walk_seconds: float = Field(
        ge=0,
        description="Total estimated walking time in seconds.",
    )
    uses_stairs: bool = Field(
        default=False,
        description="True if any part of the route uses stairs.",
    )
    uses_lift: bool = Field(
        default=False,
        description="True if any part of the route uses a lift.",
    )
    fully_sheltered: bool = Field(
        default=True,
        description=(
            "True only if every corridor on the route is covered. One "
            "uncovered stretch makes the whole route unsheltered."
        ),
    )

    @model_validator(mode="after")
    def check_edges_match_nodes(self) -> "RouteResponse":
        """A path of N nodes must be joined by exactly N - 1 edges.

        This guards the API boundary: if the router ever returned a mismatched
        path, the bug surfaces here rather than reaching the caller.
        """
        expected = len(self.nodes) - 1
        if len(self.edges) != expected:
            raise ValueError(
                f"A route with {len(self.nodes)} nodes needs {expected} edges, "
                f"but got {len(self.edges)}."
            )
        if self.steps and len(self.steps) != expected:
            raise ValueError(
                f"A route with {len(self.nodes)} nodes needs {expected} steps, "
                f"but got {len(self.steps)}."
            )
        return self

    @classmethod
    def from_route(cls, route: Route, graph: CampusGraph) -> "RouteResponse":
        """Convert a :class:`shortcut.tools.astar.Route` into this API shape.

        The internal names (``node_ids``, ``edge_ids``, ``total_seconds``) are
        renamed here, and the tuples become lists. ``nodes_expanded`` is left
        out on purpose: it describes how hard the search worked, which is
        useful in tests but is not part of the public API.

        The graph is needed to build ``steps``: a :class:`Route` records only
        ids, while a step needs the edge's own numbers and the node names that
        go into its wording.
        """
        steps: list[RouteStep] = []
        for index, edge_id in enumerate(route.edge_ids):
            edge = graph.edge_by_id(edge_id)
            from_node = graph.nodes[route.node_ids[index]]
            to_node = graph.nodes[route.node_ids[index + 1]]
            text = step_text(edge, from_node, to_node)

            steps.append(
                RouteStep(
                    step=index + 1,
                    edge_id=edge.id,
                    from_id=from_node.id,
                    to_id=to_node.id,
                    instruction=text.instruction,
                    detail=text.detail,
                    distance_m=edge.distance_m,
                    walk_seconds=edge.walk_seconds,
                    stairs=edge.stairs,
                    lift=edge.lift,
                    covered=edge.covered,
                    condition=edge.condition,
                )
            )

        return cls(
            nodes=list(route.node_ids),
            edges=list(route.edge_ids),
            steps=steps,
            total_distance_m=route.total_distance_m,
            total_walk_seconds=route.total_seconds,
            uses_stairs=route.uses_stairs,
            uses_lift=route.uses_lift,
            fully_sheltered=route.fully_sheltered,
        )


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


class ReportRequest(BaseModel):
    """Someone telling us something is wrong at a place on the map.

    The place is chosen from the map, never typed: ``target_id`` must be a
    node or edge id the graph already knows. That is what keeps two people
    reporting the same problem matchable without having to reconcile spelling.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "target_kind": "edge",
                "target_id": "Hive_B5_002",
                "condition": "blocked",
                "notes": "Barriers across the corridor by the lockers.",
            }
        },
    )

    target_kind: TargetKind = Field(
        description="Whether target_id names a node (a place) or an edge (a stretch)."
    )
    target_id: str = Field(
        min_length=1,
        max_length=100,
        description="Id of the node or edge the report is about.",
    )
    condition: Condition = Field(
        description=f"What is wrong. One of: {', '.join(CONDITIONS)}."
    )
    notes: str = Field(
        default="",
        max_length=500,
        description="Optional free text: exactly where, and anything else useful.",
    )


class ReportSummary(BaseModel):
    """One individual submission, exactly as it was sent.

    Every submission is kept and listed separately, so it is always possible
    to check what people actually reported rather than only a total.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    target_kind: TargetKind
    target_id: str
    condition: Condition
    notes: str
    status: str
    submitted_at: str
    reviewed_at: str | None = None
    photo_id: str | None = None

    @classmethod
    def from_report(cls, report: Report) -> "ReportSummary":
        return cls(
            id=report.id,
            target_kind=report.target_kind,
            target_id=report.target_id,
            condition=report.condition,
            notes=report.notes,
            status=report.status,
            submitted_at=report.submitted_at,
            reviewed_at=report.reviewed_at,
            photo_id=report.photo_id,
        )


class ReportGroupSummary(BaseModel):
    """Everyone who reported the same problem, as one row for review."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="Id for this group, used to approve or reject it.")
    target_kind: TargetKind
    target_id: str
    target_name: str = Field(
        description="Readable name of the place, for showing in the queue."
    )
    condition: Condition
    confirmations: int = Field(
        ge=1, description="How many people reported this same problem."
    )
    report_ids: list[str]
    notes: list[str] = Field(description="The notes people left, oldest first.")
    blocks_routes: bool = Field(
        description="Whether approving this would close the place to routing."
    )
    first_submitted_at: str
    latest_submitted_at: str

    @classmethod
    def from_group(
        cls, group: ReportGroup, target_name: str
    ) -> "ReportGroupSummary":
        return cls(
            key=group.key,
            target_kind=group.target_kind,
            target_id=group.target_id,
            target_name=target_name,
            condition=group.condition,
            confirmations=group.confirmations,
            report_ids=list(group.report_ids),
            notes=list(group.notes),
            blocks_routes=group.blocks_routes,
            first_submitted_at=group.first_submitted_at,
            latest_submitted_at=group.latest_submitted_at,
        )


class ReviewResult(BaseModel):
    """What an approve or reject actually did."""

    model_config = ConfigDict(extra="forbid")

    key: str
    status: str = Field(description="The state the reports were moved to.")
    reports_updated: int = Field(
        ge=0, description="How many pending reports this changed."
    )
    routing_changed: bool = Field(
        description=(
            "True if the map used for routing changed as a result. Approving "
            "a crowded report does not change routing, only warns."
        )
    )

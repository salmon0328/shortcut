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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shortcut.tools.astar import Route

__all__ = ["RouteRequest", "RouteResponse"]


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
            "example": {"origin": "Hive_B5_A", "destination": "Hive_B4_C"}
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

    # Note: origin == destination is deliberately allowed. The router handles
    # it and returns a valid empty route, so it is not an error.


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------


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
        return self

    @classmethod
    def from_route(cls, route: Route) -> "RouteResponse":
        """Convert a :class:`shortcut.tools.astar.Route` into this API shape.

        The internal names (``node_ids``, ``edge_ids``, ``total_seconds``) are
        renamed here, and the tuples become lists. ``nodes_expanded`` is left
        out on purpose: it describes how hard the search worked, which is
        useful in tests but is not part of the public API.
        """
        return cls(
            nodes=list(route.node_ids),
            edges=list(route.edge_ids),
            total_distance_m=route.total_distance_m,
            total_walk_seconds=route.total_seconds,
            uses_stairs=route.uses_stairs,
            uses_lift=route.uses_lift,
            fully_sheltered=route.fully_sheltered,
        )

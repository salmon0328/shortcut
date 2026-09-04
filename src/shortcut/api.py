"""HTTP layer for Shortcut.

This module only speaks HTTP. It receives a request, hands the work to the
deterministic routing code in :mod:`shortcut.tools.astar`, and turns the
result (or the error) into a response. The A* algorithm is never reimplemented
here.

No Claude, no Bedrock, no LangGraph, no AWS: every answer comes from
``data/campus_graph.json`` and ordinary Python.

Browser pages served from the local development frontend are allowed to call
this API; see :data:`DEV_ALLOWED_ORIGINS`.

Run it from the repository root with::

    uvicorn --app-dir src shortcut.api:app --reload

then open http://127.0.0.1:8000/docs to try the endpoints in a browser.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware

from shortcut.graph_store import CampusGraph, Edge, UnknownNodeError, load_graph
from shortcut.overrides import apply_overrides, load_overrides, set_override
from shortcut.report_store import (
    ROUTE_BLOCKING_CONDITIONS,
    ReportStatus,
    ReportStore,
)
from shortcut.schemas import (
    EdgeSummary,
    NodeSummary,
    ReportGroupSummary,
    ReportRequest,
    ReportSummary,
    ReviewResult,
    RouteRequest,
    RouteResponse,
)
from shortcut.tools.astar import (
    CostFunction,
    EdgeFilter,
    NoRouteFoundError,
    edge_seconds,
    find_route,
    prefer_lift_cost,
)

__all__ = ["app", "get_graph", "CAMPUS_GRAPH_PATH", "DEV_ALLOWED_ORIGINS"]


# --------------------------------------------------------------------------
# Where the graph lives
# --------------------------------------------------------------------------

# This file is <repo>/src/shortcut/api.py, so parents[2] is the repository
# root. Building the path from __file__ rather than from the current working
# directory means the server finds the graph no matter which folder it was
# started in.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAMPUS_GRAPH_PATH = PROJECT_ROOT / "data" / "campus_graph.json"

# Live state, kept out of the surveyed graph file on purpose. Both are created
# on demand and are not committed: they are this machine's reports, not map data.
REPORTS_PATH = PROJECT_ROOT / "data" / "reports.json"
GRAPH_OVERRIDES_PATH = PROJECT_ROOT / "data" / "graph_overrides.json"


# --------------------------------------------------------------------------
# Which browser pages may call this API
# --------------------------------------------------------------------------

# A browser refuses to let a page read a response from a different origin
# unless the server says that origin is welcome. These are the addresses the
# local development frontend runs on (5173 is Vite's default port).
#
# Listed explicitly rather than with "*": a wildcard would let any website a
# user happens to be visiting call this API from their browser. Add the real
# deployed frontend's address here when there is one.
DEV_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


# --------------------------------------------------------------------------
# Application setup
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the campus graph once, while the server starts up.

    Reading and validating the JSON on every request would be slow and would
    let the file change underneath a running server. Loading it here means the
    graph is parsed exactly once and shared by every request.

    If the file is missing or invalid, ``load_graph`` raises and the server
    refuses to start. That is deliberate: a navigation API with no map should
    fail loudly rather than answer every request with an error.
    """
    app.state.reports = ReportStore(REPORTS_PATH)
    app.state.overrides_path = GRAPH_OVERRIDES_PATH
    app.state.graph = _load_graph_with_overrides(GRAPH_OVERRIDES_PATH)
    yield


def _load_graph_with_overrides(overrides_path: Path) -> CampusGraph:
    """Read the surveyed graph, then lay any approved changes over it."""
    graph = load_graph(CAMPUS_GRAPH_PATH)
    return apply_overrides(graph, load_overrides(overrides_path))


app = FastAPI(
    title="Shortcut",
    version="0.1.0",
    summary="Deterministic indoor navigation for one campus building.",
    lifespan=lifespan,
)

# Cross-Origin Resource Sharing. This only adds response headers telling the
# browser which pages are allowed to read the answers; it does not change what
# /health or /route do, and it has no effect on non-browser callers such as
# curl, pytest or another server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    # No cookies or auth headers are used, so credentialed requests stay off.
    allow_credentials=False,
)


def get_graph(request: Request) -> CampusGraph:
    """Hand the loaded campus graph to an endpoint.

    Written as a FastAPI dependency so tests can swap in a different graph
    with ``app.dependency_overrides[get_graph] = ...`` instead of editing the
    real ``data/campus_graph.json``.
    """
    graph: CampusGraph | None = getattr(request.app.state, "graph", None)
    if graph is None:
        # Only reachable if the app is used without its lifespan having run.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The campus graph is not loaded yet. Try again shortly.",
        )
    return graph


def get_reports(request: Request) -> ReportStore:
    """Hand the report store to an endpoint.

    A dependency for the same reason as :func:`get_graph`: tests point it at a
    temporary file instead of the real ``data/reports.json``.
    """
    store: ReportStore | None = getattr(request.app.state, "reports", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The report store is not ready yet. Try again shortly.",
        )
    return store


# --------------------------------------------------------------------------
# Turning a caller's preferences into routing rules
# --------------------------------------------------------------------------


def cost_for(route_request: RouteRequest) -> CostFunction:
    """Pick the scoring function that matches the requested preference."""
    if route_request.preference == "prefer_lift":
        return prefer_lift_cost()
    return edge_seconds


def edge_filter_for(route_request: RouteRequest) -> EdgeFilter | None:
    """Build one rule that every usable edge must satisfy, or None.

    Returning ``None`` when nothing was restricted keeps the common case free
    of a pointless per-edge callback. Blocked edges are excluded by the graph
    itself, so they never need mentioning here.
    """
    rules: list[EdgeFilter] = []

    if not route_request.allow_stairs:
        rules.append(lambda edge: not edge.stairs)
    if not route_request.allow_lift:
        rules.append(lambda edge: not edge.lift)
    if route_request.sheltered_only:
        rules.append(lambda edge: edge.covered)

    if not rules:
        return None

    def passes_every_rule(edge: Edge) -> bool:
        return all(rule(edge) for rule in rules)

    return passes_every_rule


def _restrictions_in_words(route_request: RouteRequest) -> str:
    """Describe the active restrictions, for a 'no route' error message."""
    restrictions = []
    if not route_request.allow_stairs:
        restrictions.append("no stairs")
    if not route_request.allow_lift:
        restrictions.append("no lift")
    if route_request.sheltered_only:
        restrictions.append("sheltered only")
    return ", ".join(restrictions)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/health", summary="Check that the service is running")
def health() -> dict[str, str]:
    """Cheap liveness check for deployment tools and uptime monitors."""
    return {"status": "ok"}


@app.get(
    "/nodes",
    response_model=list[NodeSummary],
    summary="List every navigation point a route can start or end at",
)
def get_nodes(graph: CampusGraph = Depends(get_graph)) -> list[NodeSummary]:
    """Return every node in the graph, for building a dropdown or a picker.

    This is the single source of truth for "what places exist": it reads the
    same in-memory graph ``/route`` uses, straight from
    ``data/campus_graph.json``. A frontend that calls this instead of keeping
    its own hand-typed node list can never drift out of sync with the graph.

    Returned in the order the nodes appear in the graph file, which today
    means grouped by floor (B5, then B4).
    """
    return [NodeSummary.from_node(node) for node in graph.nodes.values()]


@app.get(
    "/edges",
    response_model=list[EdgeSummary],
    summary="List every corridor, staircase and lift",
)
def get_edges(graph: CampusGraph = Depends(get_graph)) -> list[EdgeSummary]:
    """Return every edge, for choosing one when reporting a problem.

    Like ``/nodes``, this reads the graph the server already has loaded, so a
    frontend never needs its own copy of the map.
    """
    return [
        EdgeSummary.from_edge(edge, _describe_target(graph, "edge", edge.id))
        for edge in graph.edges
    ]


@app.post(
    "/route",
    response_model=RouteResponse,
    summary="Find the quickest walkable route between two nodes",
    responses={
        404: {
            "description": (
                "The origin or destination is not a known node id, or nothing "
                "connects the two."
            )
        }
    },
)
def post_route(
    route_request: RouteRequest,
    graph: CampusGraph = Depends(get_graph),
) -> RouteResponse:
    """Return the quickest route from ``origin`` to ``destination``.

    The search itself is :func:`shortcut.tools.astar.find_route`, which
    minimises estimated walking time and never uses a blocked edge. This
    function only translates its result, or its errors, into HTTP.

    Written as a normal ``def`` rather than ``async def`` on purpose: the
    search is ordinary blocking Python, so FastAPI runs it in a worker thread
    and the server stays responsive to other requests.
    """
    try:
        route = find_route(
            graph,
            route_request.origin,
            route_request.destination,
            cost=cost_for(route_request),
            edge_filter=edge_filter_for(route_request),
        )
    except UnknownNodeError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Unknown node id {error.node_id!r}. "
                f"Check the ids in data/campus_graph.json."
            ),
        ) from error
    except NoRouteFoundError as error:
        # Say which restrictions were in force: "no route" is far less useful
        # than "no route once you ruled out stairs and lifts".
        restrictions = _restrictions_in_words(route_request)
        reason = (
            f"No route matches your choices ({restrictions})."
            if restrictions
            else "Every connecting path may be blocked."
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No walkable route from {error.origin!r} to "
                f"{error.destination!r}. {reason}"
            ),
        ) from error

    return RouteResponse.from_route(route, graph)


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
#
# The review endpoints below are NOT protected. There are no accounts yet, and
# the frontend simply offers an "admin mode" switch, so anyone who can reach
# this API can approve or reject. That is a deliberate choice for local
# development. When it needs locking down, every review endpoint already goes
# through _require_known_target and lives in this one section, so a single
# dependency added here covers all of them.


def _describe_target(graph: CampusGraph, target_kind: str, target_id: str) -> str:
    """A readable name for whatever a report points at."""
    if target_kind == "node":
        node = graph.nodes.get(target_id)
        return node.name if node else target_id

    edge = graph.edges_by_id.get(target_id)
    if edge is None:
        return target_id
    start = graph.nodes.get(edge.from_id)
    end = graph.nodes.get(edge.to_id)
    start_name = start.name if start else edge.from_id
    end_name = end.name if end else edge.to_id
    return f"{start_name} → {end_name}"


def _require_known_target(
    graph: CampusGraph, target_kind: str, target_id: str
) -> None:
    """Refuse reports about places the map does not have.

    This is what keeps reports groupable: every report names something the
    graph already knows, so two reports about one place always match exactly.
    """
    known = (
        target_id in graph.nodes
        if target_kind == "node"
        else target_id in graph.edges_by_id
    )
    if not known:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Unknown {target_kind} id {target_id!r}. Pick a place from "
                f"the map rather than typing one."
            ),
        )


@app.post(
    "/reports",
    response_model=ReportSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Report a problem at a place on the map",
)
def post_report(
    report_request: ReportRequest,
    graph: CampusGraph = Depends(get_graph),
    reports: ReportStore = Depends(get_reports),
) -> ReportSummary:
    """Record one report. It is stored pending, and changes nothing yet.

    Nothing about the map moves until an administrator approves the report, so
    a single submission can never reroute anybody.
    """
    _require_known_target(
        graph, report_request.target_kind, report_request.target_id
    )

    report = reports.add(
        target_kind=report_request.target_kind,
        target_id=report_request.target_id,
        condition=report_request.condition,
        notes=report_request.notes,
    )
    return ReportSummary.from_report(report)


@app.get(
    "/reports",
    response_model=list[ReportSummary],
    summary="List every report individually",
)
def get_reports_list(
    report_status: ReportStatus | None = None,
    reports: ReportStore = Depends(get_reports),
) -> list[ReportSummary]:
    """Every submission on its own, oldest first.

    Deliberately not grouped: this is the view for checking what people
    actually sent. Use ``/reports/groups`` for the review queue.
    """
    return [ReportSummary.from_report(r) for r in reports.all(status=report_status)]


@app.get(
    "/reports/groups",
    response_model=list[ReportGroupSummary],
    summary="Pending reports, gathered by the problem they describe",
)
def get_report_groups(
    graph: CampusGraph = Depends(get_graph),
    reports: ReportStore = Depends(get_reports),
) -> list[ReportGroupSummary]:
    """The review queue: one row per problem, most-confirmed first."""
    return [
        ReportGroupSummary.from_group(
            group, _describe_target(graph, group.target_kind, group.target_id)
        )
        for group in reports.pending_groups()
    ]


def _review_group(
    request: Request,
    key: str,
    new_status: ReportStatus,
    reports: ReportStore,
) -> ReviewResult:
    """Approve or reject one group, and apply the map change if approving."""
    groups = {group.key: group for group in reports.pending_groups()}
    group = groups.get(key)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending reports under {key!r}.",
        )

    changed = reports.set_group_status(key, new_status)

    routing_changed = False
    if new_status == "approved":
        blocks = group.condition in ROUTE_BLOCKING_CONDITIONS
        set_override(
            request.app.state.overrides_path,
            target_kind=group.target_kind,
            target_id=group.target_id,
            blocked=True if blocks else None,
            condition=group.condition,
        )
        # Rebuild from the surveyed file plus every override, so the running
        # server matches what is on disk rather than drifting from it.
        request.app.state.graph = _load_graph_with_overrides(
            request.app.state.overrides_path
        )
        routing_changed = blocks

    return ReviewResult(
        key=key,
        status=new_status,
        reports_updated=len(changed),
        routing_changed=routing_changed,
    )


@app.post(
    "/reports/groups/{key}/approve",
    response_model=ReviewResult,
    summary="Accept a reported problem and apply it to the map",
)
def approve_report_group(
    key: str,
    request: Request,
    reports: ReportStore = Depends(get_reports),
) -> ReviewResult:
    """Mark every pending report in the group approved, and update the map.

    Whether routing actually changes depends on the condition: a blockage
    closes the way, while "crowded" only puts a warning on it.
    """
    return _review_group(request, key, "approved", reports)


@app.post(
    "/reports/groups/{key}/reject",
    response_model=ReviewResult,
    summary="Dismiss a reported problem, leaving the map alone",
)
def reject_report_group(
    key: str,
    request: Request,
    reports: ReportStore = Depends(get_reports),
) -> ReviewResult:
    """Mark every pending report in the group rejected. The map is untouched."""
    return _review_group(request, key, "rejected", reports)

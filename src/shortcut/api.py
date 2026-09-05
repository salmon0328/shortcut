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

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from shortcut.crowding import crowd_waits, with_crowding
from shortcut.graph_store import CampusGraph, Edge, UnknownNodeError, load_graph
from shortcut.overrides import (
    LIVE_CONDITION_FIELDS,
    OverridesError,
    add_edge,
    add_node,
    apply_overrides,
    load_overrides,
    patch_edge,
    patch_node,
    remove_addition,
    save_overrides,
    set_override,
)
from shortcut.floorplan_store import FloorplanStore, FloorplanStoreError
from shortcut.photo_store import PhotoStore, PhotoStoreError
from shortcut.report_store import (
    ROUTE_BLOCKING_CONDITIONS,
    ReportStatus,
    ReportStore,
)
from shortcut.schemas import (
    EdgeSummary,
    FloorplanCalibration,
    FloorplanSummary,
    EdgeUpdateRequest,
    GraphChangeResult,
    NewEdgeRequest,
    NewNodeRequest,
    NodeSummary,
    NodeUpdateRequest,
    PendingChange,
    PendingChanges,
    PhotoSummary,
    ReportGroupSummary,
    ReportRequest,
    ReportSummary,
    ReviewResult,
    RouteChoices,
    RouteOption,
    RouteRequest,
    RouteResponse,
)
from shortcut.tools.astar import (
    CostFunction,
    EdgeFilter,
    NoRouteFoundError,
    Route,
    edge_seconds,
    find_alternatives,
    find_route,
    find_route_or_none,
    least_walking_cost,
    prefer_lift_cost,
    sheltered_cost,
)

__all__ = [
    "app",
    "get_graph",
    "CAMPUS_GRAPH_PATH",
    "DEV_ALLOWED_ORIGINS",
    "AI_ROUTES_ENABLED",
]


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
PHOTOS_DIR = PROJECT_ROOT / "data" / "photos"
FLOORPLANS_DIR = PROJECT_ROOT / "data" / "floorplans"


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
    app.state.photos = PhotoStore(PHOTOS_DIR)
    app.state.floorplans = FloorplanStore(FLOORPLANS_DIR)
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
    # PATCH and DELETE are here for the admin screen, which edits and removes
    # map entries. Photo uploads are multipart, so that content type is
    # allowed alongside JSON.
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
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


def get_photos(request: Request) -> PhotoStore:
    """Hand the photo store to an endpoint."""
    store: PhotoStore | None = getattr(request.app.state, "photos", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The photo store is not ready yet. Try again shortly.",
        )
    return store


def get_floorplans_store(request: Request) -> FloorplanStore:
    """Hand the floorplan store to an endpoint."""
    store: FloorplanStore | None = getattr(request.app.state, "floorplans", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The floorplan store is not ready yet. Try again shortly.",
        )
    return store


def _reload_graph(request: Request) -> CampusGraph:
    """Rebuild the running map from the survey file plus every override.

    Called after any change so the server matches what is on disk, rather than
    drifting from it.
    """
    graph = _load_graph_with_overrides(request.app.state.overrides_path)
    request.app.state.graph = graph
    return graph


# --------------------------------------------------------------------------
# Turning a caller's preferences into routing rules
# --------------------------------------------------------------------------


def cost_for(route_request: RouteRequest) -> CostFunction:
    """Pick the scoring function that matches the requested preference."""
    if route_request.preference == "prefer_lift":
        return prefer_lift_cost()
    if route_request.preference == "least_walking":
        return least_walking_cost()
    if route_request.preference == "sheltered":
        return sheltered_cost()
    return edge_seconds


def live_cost_for(
    route_request: RouteRequest, graph: CampusGraph, reports: ReportStore
) -> CostFunction:
    """The requested scoring, made slower wherever people say it is busy.

    Kept separate from :func:`cost_for` so the preference logic stays a pure
    function of the request, testable without a report store. Crowding is the
    one input that depends on what time it is.
    """
    return with_crowding(cost_for(route_request), crowd_waits(graph, reports.all()))


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
    if not route_request.allow_shuttle:
        rules.append(lambda edge: not edge.shuttle)
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
    if not route_request.allow_shuttle:
        restrictions.append("no shuttle")
    if route_request.sheltered_only:
        restrictions.append("sheltered only")
    return ", ".join(restrictions)


def _no_route_reason(route_request: RouteRequest) -> str:
    """Why there was no route, naming the choices that ruled everything out."""
    restrictions = _restrictions_in_words(route_request)
    return (
        f"No route matches your choices ({restrictions})."
        if restrictions
        else "Every connecting path may be blocked."
    )


def _photo_finder(photos: PhotoStore):
    """A lookup a route response can use without knowing about photo storage."""

    def find_photo(target_kind: str, target_id: str, facing: str | None) -> str | None:
        photo = photos.find_best(target_kind, target_id, facing)
        return f"/photos/{photo.id}/file" if photo else None

    return find_photo


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
    photos: PhotoStore = Depends(get_photos),
    reports: ReportStore = Depends(get_reports),
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
            cost=live_cost_for(route_request, graph, reports),
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No walkable route from {error.origin!r} to "
                f"{error.destination!r}. {_no_route_reason(route_request)}"
            ),
        ) from error

    return RouteResponse.from_route(route, graph, _photo_finder(photos))


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


# --------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------


def _photo_anchor(graph: CampusGraph, target_kind: str, target_id: str):
    """The node whose building and floor describe where a photo was taken."""
    if target_kind == "node":
        return graph.nodes[target_id]
    return graph.nodes[graph.edges_by_id[target_id].from_id]


@app.get(
    "/photos",
    response_model=list[PhotoSummary],
    summary="List photos, optionally only those of one place or link",
)
def get_photos_list(
    target_kind: str | None = None,
    target_id: str | None = None,
    photos: PhotoStore = Depends(get_photos),
) -> list[PhotoSummary]:
    if target_kind and target_id:
        found = photos.for_target(target_kind, target_id)
    else:
        found = photos.all()
    return [PhotoSummary.from_photo(photo) for photo in found]


@app.get(
    "/photos/{photo_id}/file",
    summary="Fetch the image itself",
)
def get_photo_file(
    photo_id: str, photos: PhotoStore = Depends(get_photos)
) -> Response:
    photo = photos.get(photo_id)
    if photo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No photo {photo_id!r}."
        )
    try:
        content = photos.read_file(photo)
    except PhotoStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    return Response(content=content, media_type=photo.content_type)


@app.post(
    "/photos",
    response_model=PhotoSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a photo of a place or a link",
)
async def post_photo(
    file: UploadFile = File(description="The image file."),
    target_kind: str = Form(description="Either 'node' or 'edge'."),
    target_id: str = Form(description="Which place or link this is a photo of."),
    location: str = Form(default="", description="Where exactly it was taken."),
    facing: str | None = Form(
        default=None,
        description=(
            "Node id being looked towards. Set this so the photo can be shown "
            "for the right direction of travel."
        ),
    ),
    caption: str = Form(default=""),
    building: str | None = Form(
        default=None, description="Defaults to the target's own building."
    ),
    floor: str | None = Form(
        default=None, description="Defaults to the target's own floor."
    ),
    graph: CampusGraph = Depends(get_graph),
    photos: PhotoStore = Depends(get_photos),
) -> PhotoSummary:
    """Store one photo against something on the map.

    The building and floor are recorded on the photo itself. They default to
    the target's own, so nobody retypes what the map already knows, but they
    are stored rather than derived: a photo should still say where it was
    taken even if the map changes around it.
    """
    if target_kind not in ("node", "edge"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="target_kind must be 'node' or 'edge'.",
        )
    _require_known_target(graph, target_kind, target_id)

    if facing is not None and facing not in graph.nodes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown node id {facing!r} for 'facing'.",
        )

    anchor = _photo_anchor(graph, target_kind, target_id)

    try:
        photo = photos.add(
            content=await file.read(),
            content_type=file.content_type or "",
            target_kind=target_kind,
            target_id=target_id,
            building=building or anchor.building,
            floor=floor or anchor.floor,
            location=location,
            facing=facing,
            caption=caption,
        )
    except PhotoStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error

    return PhotoSummary.from_photo(photo)


@app.delete(
    "/photos/{photo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a photo",
)
def delete_photo(photo_id: str, photos: PhotoStore = Depends(get_photos)) -> None:
    if not photos.delete(photo_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No photo {photo_id!r}."
        )


# --------------------------------------------------------------------------
# Editing the map
# --------------------------------------------------------------------------
#
# The same warning as the review endpoints applies here, and more strongly:
# these are NOT protected. Approving a report can only flip a flag on
# something that already exists; these change the map itself. Every one of
# them writes to the overrides file and never to data/campus_graph.json, so
# the surveyed data underneath is always recoverable by deleting that file.


def _apply_change(request: Request, write) -> CampusGraph:
    """Make a change, but keep it only if the map still builds afterwards.

    A bad edit is undone rather than left in the overrides file, where it
    would break the next startup instead of the request that caused it.
    """
    overrides_path = request.app.state.overrides_path
    before = load_overrides(overrides_path)

    write(overrides_path)
    try:
        return _reload_graph(request)
    except OverridesError as error:
        save_overrides(overrides_path, before)
        _reload_graph(request)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


@app.post(
    "/admin/nodes",
    response_model=GraphChangeResult,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new place to the map",
)
def post_admin_node(
    new_node: NewNodeRequest,
    request: Request,
    graph: CampusGraph = Depends(get_graph),
) -> GraphChangeResult:
    """Add a place, and optionally a link joining it to somewhere already there.

    A place with nothing leading to it can never be routed to, which is why
    the link is offered here rather than only as a separate step.
    """
    if new_node.id in graph.nodes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A place with id {new_node.id!r} already exists.",
        )

    edge_ids = [edge.edge_id() for edge in new_node.connections]
    for edge_id in edge_ids:
        if edge_id in graph.edges_by_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A link with id {edge_id!r} already exists.",
            )

    def write(path):
        add_node(path, new_node.id, new_node.to_fields())
        for edge in new_node.connections:
            add_edge(path, edge.edge_id(), edge.to_fields())

    # All of it or none of it: a half-added place with some of its links
    # missing would be worse than a refusal.
    updated = _apply_change(request, write)
    return GraphChangeResult(
        target_kind="node",
        target_id=new_node.id,
        created=True,
        edge_ids=edge_ids,
        node_count=len(updated.nodes),
        edge_count=len(updated.edges),
    )


@app.post(
    "/admin/edges",
    response_model=GraphChangeResult,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new link between two existing places",
)
def post_admin_edge(
    new_edge: NewEdgeRequest,
    request: Request,
    graph: CampusGraph = Depends(get_graph),
) -> GraphChangeResult:
    edge_id = new_edge.edge_id()
    if edge_id in graph.edges_by_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A link with id {edge_id!r} already exists.",
        )

    for endpoint in (new_edge.from_id, new_edge.to_id):
        if endpoint not in graph.nodes:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No place with id {endpoint!r}.",
            )

    updated = _apply_change(
        request, lambda path: add_edge(path, edge_id, new_edge.to_fields())
    )
    return GraphChangeResult(
        target_kind="edge",
        target_id=edge_id,
        created=True,
        edge_ids=[edge_id],
        node_count=len(updated.nodes),
        edge_count=len(updated.edges),
    )


@app.patch(
    "/admin/nodes/{node_id}",
    response_model=GraphChangeResult,
    summary="Change an existing place",
)
def patch_admin_node(
    node_id: str,
    changes: NodeUpdateRequest,
    request: Request,
    graph: CampusGraph = Depends(get_graph),
) -> GraphChangeResult:
    if node_id not in graph.nodes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No place {node_id!r}."
        )

    fields = changes.changed_fields()
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No changes were given.",
        )

    updated = _apply_change(request, lambda path: patch_node(path, node_id, fields))
    return GraphChangeResult(
        target_kind="node",
        target_id=node_id,
        created=False,
        node_count=len(updated.nodes),
        edge_count=len(updated.edges),
    )


@app.patch(
    "/admin/edges/{edge_id}",
    response_model=GraphChangeResult,
    summary="Change an existing link",
)
def patch_admin_edge(
    edge_id: str,
    changes: EdgeUpdateRequest,
    request: Request,
    graph: CampusGraph = Depends(get_graph),
) -> GraphChangeResult:
    if edge_id not in graph.edges_by_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No link {edge_id!r}."
        )

    fields = changes.changed_fields()
    if not fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No changes were given.",
        )

    updated = _apply_change(request, lambda path: patch_edge(path, edge_id, fields))
    return GraphChangeResult(
        target_kind="edge",
        target_id=edge_id,
        created=False,
        node_count=len(updated.nodes),
        edge_count=len(updated.edges),
    )


@app.delete(
    "/admin/additions/{target_kind}/{target_id}",
    response_model=GraphChangeResult,
    summary="Remove something an administrator added",
)
def delete_admin_addition(
    target_kind: str,
    target_id: str,
    request: Request,
    photos: PhotoStore = Depends(get_photos),
) -> GraphChangeResult:
    """Delete an added place or link, along with its photos.

    Only additions can be removed. Surveyed places and links stay: closing one
    is what ``blocked`` is for, and deleting it here would put the map out of
    step with the building somebody actually measured.
    """
    if target_kind not in ("node", "edge"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kind must be 'node' or 'edge'.",
        )

    overrides_path = request.app.state.overrides_path
    if not remove_addition(overrides_path, target_kind, target_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{target_id!r} was not added through the app, so it cannot be "
                f"removed here. Surveyed places and links can only be blocked."
            ),
        )

    photos.delete_for_target(target_kind, target_id)
    updated = _reload_graph(request)
    return GraphChangeResult(
        target_kind=target_kind,
        target_id=target_id,
        created=False,
        node_count=len(updated.nodes),
        edge_count=len(updated.edges),
    )


# --------------------------------------------------------------------------
# Floorplans
# --------------------------------------------------------------------------
#
# Nothing routes on these. A floorplan is what lets a frontend draw a route
# instead of listing it, and none of the images or coordinates have been
# collected yet, so most of this exists so the space is ready.


@app.get(
    "/floorplans",
    response_model=list[FloorplanSummary],
    summary="List floorplans, optionally for one floor",
)
def get_floorplans(
    building: str | None = None,
    floor: str | None = None,
    floorplans: FloorplanStore = Depends(get_floorplans_store),
) -> list[FloorplanSummary]:
    if building and floor:
        plan = floorplans.for_floor(building, floor)
        return [FloorplanSummary.from_floorplan(plan)] if plan else []
    return [FloorplanSummary.from_floorplan(plan) for plan in floorplans.all()]


@app.get(
    "/floorplans/{floorplan_id}/file",
    summary="Fetch a floorplan image",
)
def get_floorplan_file(
    floorplan_id: str, floorplans: FloorplanStore = Depends(get_floorplans_store)
) -> Response:
    plan = floorplans.get(floorplan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No floorplan {floorplan_id!r}.",
        )
    try:
        content = floorplans.read_file(plan)
    except FloorplanStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    return Response(content=content, media_type=plan.content_type)


@app.post(
    "/floorplans",
    response_model=FloorplanSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a floorplan for one floor of one building",
)
async def post_floorplan(
    file: UploadFile = File(description="The floorplan image."),
    building: str = Form(description="Which building this is a plan of."),
    floor: str = Form(description="Which floor."),
    origin_x_m: float | None = Form(
        default=None, description="Map x at the image's top-left corner."
    ),
    origin_y_m: float | None = Form(
        default=None, description="Map y at the image's top-left corner."
    ),
    metres_per_pixel: float | None = Form(
        default=None, description="How much ground one pixel covers."
    ),
    note: str = Form(default=""),
    floorplans: FloorplanStore = Depends(get_floorplans_store),
) -> FloorplanSummary:
    """Store one floorplan image.

    The measurements are optional: a plan can be uploaded now and calibrated
    later, and stays marked uncalibrated until it is.
    """
    try:
        plan = floorplans.add(
            content=await file.read(),
            content_type=file.content_type or "",
            building=building,
            floor=floor,
            origin_x_m=origin_x_m,
            origin_y_m=origin_y_m,
            metres_per_pixel=metres_per_pixel,
            note=note,
        )
    except FloorplanStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    return FloorplanSummary.from_floorplan(plan)


@app.patch(
    "/floorplans/{floorplan_id}",
    response_model=FloorplanSummary,
    summary="Set where the map sits on a floorplan image",
)
def patch_floorplan(
    floorplan_id: str,
    calibration: FloorplanCalibration,
    floorplans: FloorplanStore = Depends(get_floorplans_store),
) -> FloorplanSummary:
    try:
        plan = floorplans.calibrate(
            floorplan_id,
            origin_x_m=calibration.origin_x_m,
            origin_y_m=calibration.origin_y_m,
            metres_per_pixel=calibration.metres_per_pixel,
            note=calibration.note,
        )
    except FloorplanStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error

    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No floorplan {floorplan_id!r}.",
        )
    return FloorplanSummary.from_floorplan(plan)


@app.delete(
    "/floorplans/{floorplan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a floorplan",
)
def delete_floorplan(
    floorplan_id: str, floorplans: FloorplanStore = Depends(get_floorplans_store)
) -> None:
    if not floorplans.delete(floorplan_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No floorplan {floorplan_id!r}.",
        )


# --------------------------------------------------------------------------
# Other ways round
# --------------------------------------------------------------------------

# Which measure each preference is trying to keep down, and which one an
# alternative should therefore improve on. Asking for the fastest route and
# being offered a slightly slower one that walks you less is useful; being
# offered one that is worse at both is not.
_TRADE_AXIS = {
    "fastest": ("seconds", "walking"),
    "prefer_lift": ("seconds", "walking"),
    "least_walking": ("walking", "seconds"),
    "sheltered": ("seconds", "walking"),
}

_PREFERENCE_LABELS = {
    "fastest": "Fastest",
    "prefer_lift": "Less climbing",
    "least_walking": "Less walking",
    "sheltered": "Driest",
}


def _describe_trade(option: Route, against: Route) -> str:
    """Say plainly what an alternative gives and what it costs."""
    parts: list[str] = []

    walking_saved = against.walking_distance_m - option.walking_distance_m
    if abs(walking_saved) >= 1:
        word = "less" if walking_saved > 0 else "more"
        parts.append(f"{abs(walking_saved):.0f} m {word} walking")

    seconds_saved = against.total_seconds - option.total_seconds
    if abs(seconds_saved) >= 1:
        word = "faster" if seconds_saved > 0 else "slower"
        parts.append(f"{abs(seconds_saved) / 60:.0f} min {word}"
                     if abs(seconds_saved) >= 60
                     else f"{abs(seconds_saved):.0f} sec {word}")

    return ", ".join(parts) if parts else "About the same, a different way round"


# Ways a route can differ in *kind* rather than in numbers.
#
# find_alternatives only offers a route that beats the chosen one on a
# measure - fewer metres, fewer seconds. That misses the comparison people
# most often want. Asked for the quickest way, it will never mention the
# step-free one, because going by lift walks you further and takes longer:
# it loses on every number it is scored by, and is still exactly what someone
# with a suitcase or a knee injury needs to see.
#
# So these are offered on the strength of what they *are*. Each names a
# property, how to say it, and the rule an edge must satisfy to keep it.
_CHARACTERISTIC_OPTIONS: tuple[tuple[str, str, EdgeFilter], ...] = (
    ("uses_stairs", "Step-free", lambda edge: not edge.stairs),
    ("exposed", "Stays dry", lambda edge: edge.covered),
)


def _lacks(route: Route, graph: CampusGraph, characteristic: str) -> bool:
    """Whether ``route`` fails to have the property, so it is worth offering."""
    edges = [graph.edge_by_id(edge_id) for edge_id in route.edge_ids]
    if characteristic == "uses_stairs":
        return any(edge.stairs for edge in edges)
    return any(not edge.covered for edge in edges)


def _characteristic_alternatives(
    route_request: RouteRequest,
    graph: CampusGraph,
    best: Route,
    cost: CostFunction,
    edge_filter: EdgeFilter | None,
) -> list[tuple[Route, str]]:
    """Routes worth showing because of what they are, not what they score.

    Only ever returns a route the chosen one is not already, and never the
    same path twice, so nothing is offered as an alternative to itself.
    """
    found: list[tuple[Route, str]] = []
    seen = {best.node_ids}

    for characteristic, label, rule in _CHARACTERISTIC_OPTIONS:
        if not _lacks(best, graph, characteristic):
            continue

        def passes(edge: Edge, rule: EdgeFilter = rule) -> bool:
            return rule(edge) and (edge_filter(edge) if edge_filter else True)

        candidate = find_route_or_none(
            graph,
            route_request.origin,
            route_request.destination,
            cost=cost,
            edge_filter=passes,
        )
        if candidate is None or candidate.node_ids in seen:
            continue
        seen.add(candidate.node_ids)
        found.append((candidate, label))

    return found


@app.post(
    "/route/options",
    response_model=RouteChoices,
    summary="Find a route, plus other ways round worth considering",
    responses={404: {"description": "Unknown node id, or nothing connects the two."}},
)
def post_route_options(
    route_request: RouteRequest,
    graph: CampusGraph = Depends(get_graph),
    photos: PhotoStore = Depends(get_photos),
    reports: ReportStore = Depends(get_reports),
) -> RouteChoices:
    """Return the best route for the request, and up to two alternatives.

    An alternative is only offered when it beats the chosen route on the
    measure that route was *not* optimising: ask for the fastest way and you
    are shown one that walks you less, ask to walk less and you are shown one
    that is quicker.
    """
    cost = live_cost_for(route_request, graph, reports)
    edge_filter = edge_filter_for(route_request)

    try:
        best = find_route(
            graph,
            route_request.origin,
            route_request.destination,
            cost=cost,
            edge_filter=edge_filter,
        )
    except UnknownNodeError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown node id {error.node_id!r}.",
        ) from error
    except NoRouteFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No walkable route from {error.origin!r} to "
                f"{error.destination!r}. {_no_route_reason(route_request)}"
            ),
        ) from error

    keep_axis, trade_axis = _TRADE_AXIS[route_request.preference]
    others = find_alternatives(
        graph,
        route_request.origin,
        route_request.destination,
        best,
        trade_axis=trade_axis,
        keep_axis=keep_axis,
        cost=cost,
        edge_filter=edge_filter,
    )

    def as_option(route: Route, label: str, why: str) -> RouteOption:
        return RouteOption(
            label=label,
            why=why,
            route=RouteResponse.from_route(route, graph, _photo_finder(photos)),
        )

    return RouteChoices(
        primary=as_option(
            best,
            _PREFERENCE_LABELS[route_request.preference],
            "The best match for what you asked for",
        ),
        alternatives=[
            as_option(
                route,
                "Less walking" if trade_axis == "walking" else "Quicker",
                _describe_trade(route, best),
            )
            for route in others
        ]
        + [
            as_option(route, label, _describe_trade(route, best))
            for route, label in _characteristic_alternatives(
                route_request, graph, best, cost, edge_filter
            )
        ],
    )


# --------------------------------------------------------------------------
# What is waiting to be folded into the survey
# --------------------------------------------------------------------------


def _split_fields(fields: dict) -> tuple[list[str], list[str]]:
    """Sort a change's fields into what the survey keeps and what stays live."""
    graduating = sorted(f for f in fields if f not in LIVE_CONDITION_FIELDS)
    live = sorted(f for f in fields if f in LIVE_CONDITION_FIELDS)
    return graduating, live


def _pending_change(
    graph: CampusGraph, kind: str, change: str, target_id: str, fields: dict
) -> PendingChange:
    graduating, live = _split_fields(fields)
    return PendingChange(
        kind=kind,
        change=change,
        id=target_id,
        label=_describe_target(graph, kind, target_id),
        fields=fields,
        graduating_fields=graduating,
        live_fields=live,
    )


@app.get(
    "/admin/pending",
    response_model=PendingChanges,
    summary="Everything changed since the survey, not yet folded into it",
)
def get_pending_changes(
    request: Request, graph: CampusGraph = Depends(get_graph)
) -> PendingChanges:
    """List what is sitting in the overrides file.

    Read-only. Moving any of it into ``data/campus_graph.json`` is done by
    running ``scripts/graduate_overrides.py``, so that a change to a version
    controlled file stays a deliberate act reviewed as a git diff rather than
    a button pressed while browsing.
    """
    overrides = load_overrides(request.app.state.overrides_path)

    changes: list[PendingChange] = []
    for node_id, fields in overrides["added_nodes"].items():
        changes.append(_pending_change(graph, "node", "added", node_id, fields))
    for edge_id, fields in overrides["added_edges"].items():
        changes.append(_pending_change(graph, "edge", "added", edge_id, fields))
    for node_id, fields in overrides["nodes"].items():
        changes.append(_pending_change(graph, "node", "edited", node_id, fields))
    for edge_id, fields in overrides["edges"].items():
        changes.append(_pending_change(graph, "edge", "edited", edge_id, fields))

    return PendingChanges(
        changes=changes,
        total=len(changes),
        graduating=sum(1 for c in changes if c.graduating_fields),
        live_only=sum(1 for c in changes if not c.graduating_fields),
    )


# --------------------------------------------------------------------------
# The agentic layer, if it is installed
# --------------------------------------------------------------------------
#
# Guarded on purpose. The AI dependencies live in a separate requirements
# file, and a machine with none of them must still serve every endpoint above
# and still pass the whole core test suite. A missing dependency degrades to
# "no /ai routes", never to a server that will not start.
#
# ImportError only, never a bare except: a genuine mistake inside the AI code
# has to surface as an error, not disappear into "the AI is unavailable".
#
# The router is built with this module's own get_graph handed in, rather than
# imported from here by the router, which keeps the dependency pointing one
# way and lets tests override the graph for /ai/* exactly as they do for
# /route. See shortcut/ai/routes.py.

try:
    from shortcut.ai.routes import build_ai_router
except ImportError:  # pragma: no cover - only on a machine without the extras
    AI_ROUTES_ENABLED = False
else:
    app.include_router(build_ai_router(graph_dependency=get_graph))
    AI_ROUTES_ENABLED = True

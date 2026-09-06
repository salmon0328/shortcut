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

import logging

import pymupdf
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

from shortcut.candidate_store import Candidate, CandidateStore
from shortcut.crowding import crowd_waits, with_crowding
from shortcut.dotenv import load_dotenv
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
    remove_entity,
    save_overrides,
    set_override,
)
from shortcut.floorplan_build import build_drafts
from shortcut.floorplan_store import FloorplanStore, FloorplanStoreError
from shortcut.import_source_store import ImportSourceError, ImportSourceStore
from shortcut.nodemap import node_id_for, read_uploaded_pdf
from shortcut.photo_store import PhotoStore, PhotoStoreError
from shortcut.survey_import import (
    PlanCalibration,
    _building_and_floor,
    read_candidates,
)
from shortcut.report_store import (
    ROUTE_BLOCKING_CONDITIONS,
    ReportStatus,
    ReportStore,
)
from shortcut.schemas import (
    BulkApprovalResult,
    CandidateReviewResult,
    CandidateUpdateRequest,
    EdgeSummary,
    FloorplanCalibration,
    FloorplanSummary,
    EdgeUpdateRequest,
    GraphChangeResult,
    ImportCandidate,
    ImportSummary,
    NewEdgeRequest,
    NewNodeRequest,
    NodeSummary,
    NodeUpdateRequest,
    PendingChange,
    PendingChanges,
    PhotoSummary,
    PhotoUpdateRequest,
    ReportGroupSummary,
    ReportRequest,
    ReportSummary,
    ReviewResult,
    RouteChoices,
    SkippedCandidate,
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
    "DOTENV_PATH",
    "DEV_ALLOWED_ORIGINS",
    "AI_ROUTES_ENABLED",
]

logger = logging.getLogger(__name__)


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
IMPORT_CANDIDATES_PATH = PROJECT_ROOT / "data" / "import_candidates.json"
IMPORT_SOURCES_DIR = PROJECT_ROOT / "data" / "import_sources"
PHOTOS_DIR = PROJECT_ROOT / "data" / "photos"
FLOORPLANS_DIR = PROJECT_ROOT / "data" / "floorplans"

# Optional settings, read once at startup and only ever filling in what the
# real environment leaves unset. A module-level path so tests can point it
# somewhere empty and stay on local disk whatever a developer's own file says.
DOTENV_PATH = PROJECT_ROOT / ".env"


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

    ``.env`` is read first, before anything that might depend on it: the
    photo and floorplan stores choose between local disk and S3 when they
    are built, so a bucket named in the file has to be in the environment by
    then.
    """
    applied = load_dotenv(DOTENV_PATH)
    if applied:
        logger.info("Read %s from %s", ", ".join(applied), DOTENV_PATH)

    app.state.reports = ReportStore(REPORTS_PATH)
    app.state.photos = PhotoStore(PHOTOS_DIR)
    app.state.candidates = CandidateStore(IMPORT_CANDIDATES_PATH)
    app.state.import_sources = ImportSourceStore(IMPORT_SOURCES_DIR)
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


def get_candidates(request: Request) -> CandidateStore:
    """Hand the queue of unreviewed candidates to an endpoint."""
    store: CandidateStore | None = getattr(request.app.state, "candidates", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The import queue is not ready yet. Try again shortly.",
        )
    return store


def get_import_sources(request: Request) -> ImportSourceStore:
    """Hand the store of uploaded drawings to an endpoint."""
    store: ImportSourceStore | None = getattr(request.app.state, "import_sources", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The import store is not ready yet. Try again shortly.",
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
    """A lookup a route response can use without knowing about photo storage.

    Photos are a nicety on top of a route; the directions are the answer. So
    a photo store that cannot be reached - expired AWS credentials, S3 having
    a bad day, an unreadable index - costs the walker their pictures and
    nothing else. Letting it raise here would turn a decorative failure into
    a building with no working navigation at all.

    The index is read once, here, rather than once per step: a route can ask
    about several steps, and each ask tries an edge photo and then a node
    photo, so without this a single request could hit the store several
    times over for what is really one snapshot of "what photos exist".
    """
    try:
        snapshot = photos.all()
    except Exception:
        logger.exception("Could not read the photo index; routing on without photos.")
        snapshot = []

    def find_photo(target_kind: str, target_id: str, facing: str | None) -> str | None:
        photo = PhotoStore.best_of(snapshot, target_kind, target_id, facing)
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


# An uploaded image never changes. Replacing a floorplan or a photo stores a
# new one under a new id and leaves the old file alone, so a URL that has
# answered once will answer the same for ever - which is exactly the condition
# "immutable" describes, and it is worth saying out loud.
#
# It matters more than it looks. These files come from S3 now, and the map
# panel asks for a floorplan every time a route is drawn: without this the
# browser re-downloads the better part of a megabyte on each one and the map
# arrives about three seconds late, looking broken rather than slow. The
# server-side cache below covers the first request; this covers all the rest.
IMMUTABLE = "public, max-age=31536000, immutable"

#: Images already fetched from the store, kept by id.
#:
#: Unbounded per process, which is safe here for a reason worth stating: an id
#: is a fresh uuid per upload and is never reused, and both endpoints look the
#: image up in the store *before* consulting this - so a deleted image still
#: answers 404 whatever is cached, and the worst a stale entry costs is the
#: memory until restart. The deletes below drop their entry anyway, because
#: leaving rubbish behind on purpose invites somebody to rely on it.
_image_cache: dict[str, bytes] = {}


def _cached_bytes(key: str, read) -> bytes:
    """Read an image once per process, however many routes ask for it."""
    if key not in _image_cache:
        _image_cache[key] = read()
    return _image_cache[key]


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
        content = _cached_bytes(f"photo:{photo.id}", lambda: photos.read_file(photo))
    except PhotoStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    return Response(
        content=content,
        media_type=photo.content_type,
        headers={"Cache-Control": IMMUTABLE},
    )


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


@app.patch(
    "/photos/{photo_id}",
    response_model=PhotoSummary,
    summary="Change what is recorded about a photo",
    responses={404: {"description": "No photo with that id."}},
)
def patch_photo(
    photo_id: str,
    changes: PhotoUpdateRequest,
    photos: PhotoStore = Depends(get_photos),
    graph: CampusGraph = Depends(get_graph),
) -> PhotoSummary:
    """Label a photo after the fact - above all, which way it faces.

    The direction is checked against the map here rather than trusted, because
    a photo facing a place that does not exist is one ``best_of`` will never
    match and nobody will ever notice is broken.
    """
    if changes.facing is not None and changes.facing not in graph.nodes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown node id {changes.facing!r} to face.",
        )

    updated = photos.update_details(
        photo_id,
        caption=changes.caption,
        location=changes.location,
        facing=changes.facing,
        clear_facing=changes.clear_facing,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No photo {photo_id!r}."
        )
    return PhotoSummary.from_photo(updated)


@app.delete(
    "/photos/{photo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a photo",
)
def delete_photo(photo_id: str, photos: PhotoStore = Depends(get_photos)) -> None:
    _image_cache.pop(f"photo:{photo_id}", None)
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


def _delete_target(
    request: Request,
    graph: CampusGraph,
    photos: PhotoStore,
    target_kind: str,
    target_id: str,
    exists: bool,
) -> GraphChangeResult:
    """Remove a place or link, whether it was added here or surveyed.

    Two different removals wearing one name. Something added through the app
    is simply un-added - the entry leaves the overrides file and there is no
    trace, because there was never anything in the survey to contradict.
    Something surveyed gets a tombstone instead: the survey is what a person
    measured in the building and the app does not write it, so the deletion
    lives alongside every other pending change until somebody graduates it and
    reads the diff.
    """
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {'place' if target_kind == 'node' else 'link'} {target_id!r}.",
        )

    def write(path):
        # Un-adding beats a tombstone where both would work: it leaves the
        # overrides file as if the thing had never been added, rather than
        # carrying a note about deleting something that was never surveyed.
        if not remove_addition(path, target_kind, target_id):
            remove_entity(path, target_kind, target_id)

    updated = _apply_change(request, write)

    # Pictures of somewhere that no longer exists are just files nobody can
    # reach, and they would come back attached to the id if it were ever
    # reused.
    photos.delete_for_target(target_kind, target_id)

    return GraphChangeResult(
        target_kind=target_kind,
        target_id=target_id,
        created=False,
        node_count=len(updated.nodes),
        edge_count=len(updated.edges),
    )


@app.delete(
    "/admin/nodes/{node_id}",
    response_model=GraphChangeResult,
    summary="Remove a place, and every link that led to it",
    responses={404: {"description": "No place with that id."}},
)
def delete_admin_node(
    node_id: str,
    request: Request,
    graph: CampusGraph = Depends(get_graph),
    photos: PhotoStore = Depends(get_photos),
) -> GraphChangeResult:
    """Delete a place. Links to it go too, because they would point at nothing.

    That cascade is not a convenience: an edge whose end does not exist makes
    the graph refuse to build, so leaving them would break the map rather than
    leave it untidy.
    """
    return _delete_target(
        request, graph, photos, "node", node_id, node_id in graph.nodes
    )


@app.delete(
    "/admin/edges/{edge_id}",
    response_model=GraphChangeResult,
    summary="Remove a link",
    responses={404: {"description": "No link with that id."}},
)
def delete_admin_edge(
    edge_id: str,
    request: Request,
    graph: CampusGraph = Depends(get_graph),
    photos: PhotoStore = Depends(get_photos),
) -> GraphChangeResult:
    return _delete_target(
        request, graph, photos, "edge", edge_id, edge_id in graph.edges_by_id
    )


@app.delete(
    "/admin/additions/{target_kind}/{target_id}",
    response_model=GraphChangeResult,
    summary="Remove something an administrator added",
    deprecated=True,
)
def delete_admin_addition(
    target_kind: str,
    target_id: str,
    request: Request,
    graph: CampusGraph = Depends(get_graph),
    photos: PhotoStore = Depends(get_photos),
) -> GraphChangeResult:
    """The older, additions-only removal. Kept so nothing pointed at it breaks.

    Prefer ``DELETE /admin/nodes/{id}`` and ``DELETE /admin/edges/{id}``, which
    remove surveyed places and links too.
    """
    if target_kind not in ("node", "edge"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kind must be 'node' or 'edge'.",
        )
    exists = (
        target_id in graph.nodes
        if target_kind == "node"
        else target_id in graph.edges_by_id
    )
    return _delete_target(request, graph, photos, target_kind, target_id, exists)


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
    """List the plans, or say plainly that the store cannot be reached.

    An unreachable store is not the same as a floor nobody has surveyed, and
    the difference is the whole message. Returning an empty list would have
    the map report "no floorplan for this floor yet" and send somebody off to
    upload one that is already there; a bare 500 tells them only that
    something broke. Both hide the usual cause, which is that a set of
    temporary AWS credentials quietly expired.
    """
    try:
        if building and floor:
            plan = floorplans.for_floor(building, floor)
            return [FloorplanSummary.from_floorplan(plan)] if plan else []
        return [FloorplanSummary.from_floorplan(plan) for plan in floorplans.all()]
    except Exception as error:  # noqa: BLE001 - any storage failure reads the same
        logger.warning("floorplan store unreachable: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The floorplan store could not be reached, so the map cannot be "
                "drawn. Routes and directions are unaffected. If this machine "
                "uses temporary AWS credentials, they have most likely expired."
            ),
        ) from error


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
        content = _cached_bytes(f"plan:{plan.id}", lambda: floorplans.read_file(plan))
    except FloorplanStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    except Exception as error:  # noqa: BLE001 - see get_floorplans
        logger.warning("floorplan image unreachable: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The floorplan image could not be fetched from storage.",
        ) from error
    return Response(
        content=content,
        media_type=plan.content_type,
        headers={"Cache-Control": IMMUTABLE},
    )


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
    _image_cache.pop(f"plan:{floorplan_id}", None)
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
    if change == "removed":
        # A removal graduates whatever it carries, and it carries almost
        # nothing - the fact is the deletion itself. Sorting it by its fields
        # would count it as live-only and let the summary say nothing is
        # waiting for the survey while a place sits deleted.
        graduating, live = ["removed"], []
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

    # Deletions are pending changes like any other, and the one kind that
    # cannot be seen by looking at the map - the place is simply not there any
    # more, with nothing to click on and ask about. Listing them here is the
    # only place somebody can notice one before it graduates.
    for node_id, fields in overrides.get("removed_nodes", {}).items():
        changes.append(_pending_change(graph, "node", "removed", node_id, fields))
    for edge_id, fields in overrides.get("removed_edges", {}).items():
        changes.append(_pending_change(graph, "edge", "removed", edge_id, fields))

    return PendingChanges(
        changes=changes,
        total=len(changes),
        graduating=sum(1 for c in changes if c.graduating_fields),
        live_only=sum(1 for c in changes if not c.graduating_fields),
    )


# --------------------------------------------------------------------------
# Reading a drawing into changes somebody reviews
# --------------------------------------------------------------------------
#
# The lifecycle, and why it has the shape it does:
#
#   upload   -> candidates, inert. They route nobody and appear in no search,
#               because nothing has checked that the drawing was read right.
#   approve  -> the candidate moves into the overrides file, exactly as if
#               somebody had typed it into the "Add a place" form. It is live
#               from that moment: routable, searchable, and listed by
#               /admin/pending with everything else awaiting graduation.
#   reject   -> deleted. It was never live, so there is nothing to undo.
#   graduate -> unchanged: scripts/graduate_overrides.py, read as a git diff.
#
# So this adds one step in front of the existing machinery rather than a
# second way into the map.


def _plan_calibrations(
    floorplans: FloorplanStore,
) -> dict[tuple[str, str], PlanCalibration]:
    """The fitted scale for every floor that has a measured plan.

    Positions come off a drawing as a fraction of the plan; the map works in
    metres. This is what converts between them, and a floor missing from here
    is a floor whose places arrive without a position - which is the honest
    outcome, since nothing on record says where its plan sits on the map.

    The image is measured rather than assumed: ``metres_per_pixel`` was fitted
    against the plan as uploaded, so the fraction has to be multiplied by that
    same image's size and no other.
    """
    calibrations: dict[tuple[str, str], PlanCalibration] = {}
    for plan in floorplans.all():
        if not plan.is_calibrated:
            continue
        try:
            image = pymupdf.Pixmap(floorplans.read_file(plan))
        except Exception:  # noqa: BLE001 - an unreadable plan costs positions, not the upload
            logger.exception("Could not measure floorplan %s; places on %s %s "
                             "will arrive without a position.",
                             plan.id, plan.building, plan.floor)
            continue
        calibrations[(plan.building, plan.floor)] = PlanCalibration(
            origin_x_m=plan.origin_x_m,
            origin_y_m=plan.origin_y_m,
            metres_per_pixel=plan.metres_per_pixel,
            width_px=image.width,
            height_px=image.height,
        )
    return calibrations


def _candidate_label(graph: CampusGraph, candidate: Candidate) -> str:
    """How to name a candidate in a list."""
    if candidate.kind == "floorplan":
        fields = candidate.fields
        where = " · ".join(
            part for part in (fields.get("building"), fields.get("floor")) if part
        )
        return f"Floorplan for {where}"

    if candidate.kind == "node":
        fields = candidate.fields
        where = " · ".join(part for part in (fields.get("building"), fields.get("floor")) if part)
        return f"{fields.get('name', candidate.target_id)}{f' ({where})' if where else ''}"

    def name_of(node_id: str) -> str:
        node = graph.nodes.get(node_id)
        return node.name if node else node_id

    return f"{name_of(candidate.fields['from'])} → {name_of(candidate.fields['to'])}"


def _blockers(graph: CampusGraph, candidate: Candidate, waiting: list[Candidate]) -> list[str]:
    """Which candidates have to be approved before this one can be.

    A link needs the places at both its ends to exist. Reported rather than
    silently reordered, so the screen can grey out the button and say why.
    """
    if candidate.kind != "edge":
        return []
    by_target = {other.target_id: other.id for other in waiting if other.kind == "node"}
    return [
        by_target[end]
        for end in (candidate.fields["from"], candidate.fields["to"])
        if end not in graph.nodes and end in by_target
    ]


def _as_import_candidate(
    graph: CampusGraph, candidate: Candidate, waiting: list[Candidate]
) -> ImportCandidate:
    return ImportCandidate(
        id=candidate.id,
        kind=candidate.kind,
        target_id=candidate.target_id,
        label=_candidate_label(graph, candidate),
        fields=candidate.fields,
        source=candidate.source,
        marks=list(candidate.marks),
        disagrees_with_survey=candidate.disagrees_with_survey,
        name_is_a_stand_in=candidate.name_is_a_stand_in,
        blocked_by=_blockers(graph, candidate, waiting),
    )


@app.post(
    "/admin/import",
    response_model=ImportSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Read one or more drawings into changes to review",
    responses={422: {"description": "A file could not be read as a drawing."}},
)
async def post_admin_import(
    files: list[UploadFile] = File(..., description="Node-map PDFs to read."),
    graph: CampusGraph = Depends(get_graph),
    candidates: CandidateStore = Depends(get_candidates),
    floorplans: FloorplanStore = Depends(get_floorplans_store),
    sources: ImportSourceStore = Depends(get_import_sources),
) -> ImportSummary:
    """Read what the drawings say, and queue whatever the map does not have.

    Nothing is added to the map here. Every file is read, and what it found
    waits for a person - including the lines it could not settle, which are
    reported in the reviewer's own words rather than guessed at.
    """
    calibrations = _plan_calibrations(floorplans)

    filenames: list[str] = []
    queued: list[dict] = []
    unsettled: list[str] = []
    already_known = 0
    found = 0

    for upload in files:
        content = await upload.read()
        name = upload.filename or "a drawing"
        try:
            extractions = read_uploaded_pdf(content)
        except Exception as error:  # noqa: BLE001 - any unreadable file, reported
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{name} could not be read as a drawing: {error}",
            ) from error

        reading = read_candidates(
            graph, extractions, filename=name, calibrations=calibrations
        )

        # A floor's plan is composed from every crop the drawing puts it on,
        # and measured from the links drawn across it. Kept as a draft the
        # same way places and links are: it is a reading of a drawing, and a
        # reading is something a person should see before it lands.
        floor_of = {
            node_id_for(place.name): _building_and_floor(place.name, extraction.floor)
            for extraction in extractions
            if extraction.floor
            for place in extraction.places
        }
        drafts, plan_notes = build_drafts(
            content,
            extractions,
            floor_of,
            filename=name,
            # Measured, not drawn. A review screen wants the numbers, and
            # rasterising every floor on upload spends a second producing
            # images most of which are about to be thrown away.
            compose=False,
            already_have={
                (plan.building, plan.floor)
                for plan in floorplans.all()
                if plan.is_calibrated
            },
        )
        unsettled.extend(plan_notes)

        # The drawing itself is stored, not the images built from it: a
        # megabyte of PNG per floor does not belong in the queue's JSON, and
        # rebuilding from the PDF on approval gives the same plan every time.
        source_id = sources.add(content) if drafts else None
        for draft in drafts:
            queued.append(
                {
                    "kind": "floorplan",
                    "target_id": f"{draft.building}|{draft.floor}",
                    "fields": {
                        "building": draft.building,
                        "floor": draft.floor,
                        "source_id": source_id,
                        "metres_per_pixel": draft.metres_per_pixel,
                        "width_px": draft.width_px,
                        "height_px": draft.height_px,
                        "measures_m": list(draft.measures),
                        "made_of": draft.made_of,
                        "links_used": draft.links_used,
                        "spread": round(draft.spread, 1),
                        "well_conditioned": draft.well_conditioned,
                        "replaces_existing": draft.replaces_existing,
                    },
                    "source": draft.source,
                }
            )
        found += len(drafts)
        filenames.append(name)
        unsettled.extend(reading.unsettled)
        already_known += reading.already_known_nodes + reading.already_known_edges
        found += len(reading.nodes) + len(reading.edges)

        for node in reading.nodes:
            queued.append(
                {
                    "kind": "node",
                    "target_id": node.node_id,
                    "fields": node.as_fields(),
                    "source": node.source,
                    "name_is_a_stand_in": node.name_is_a_stand_in,
                }
            )
        for edge in reading.edges:
            queued.append(
                {
                    "kind": "edge",
                    "target_id": edge.edge_id,
                    "fields": edge.as_fields(),
                    "source": edge.source,
                    "marks": edge.marks,
                    "disagrees_with_survey": edge.disagrees_with_survey,
                }
            )

    added = candidates.add_many(queued)
    waiting = candidates.all()
    return ImportSummary(
        filenames=filenames,
        added=len(added),
        already_known=already_known,
        already_waiting=found - len(added),
        unsettled=unsettled,
        candidates=[_as_import_candidate(graph, c, waiting) for c in waiting],
    )


@app.get(
    "/admin/import/candidates",
    response_model=list[ImportCandidate],
    summary="Everything read from a drawing and not yet decided on",
)
def get_import_candidates(
    graph: CampusGraph = Depends(get_graph),
    candidates: CandidateStore = Depends(get_candidates),
) -> list[ImportCandidate]:
    waiting = candidates.all()
    return [_as_import_candidate(graph, c, waiting) for c in waiting]


@app.patch(
    "/admin/import/candidates/{candidate_id}",
    response_model=ImportCandidate,
    summary="Correct a candidate before approving it",
    responses={404: {"description": "No candidate with that id."}},
)
def patch_import_candidate(
    candidate_id: str,
    changes: CandidateUpdateRequest,
    graph: CampusGraph = Depends(get_graph),
    candidates: CandidateStore = Depends(get_candidates),
) -> ImportCandidate:
    """Change what would be written, without writing any of it yet.

    This is where a stand-in name becomes a real one, and where somebody who
    knows the building says a corridor is stairs or is rained on - the two
    things a drawing is least able to say for itself.
    """
    candidate = candidates.get(candidate_id)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No candidate {candidate_id!r}.",
        )

    try:
        wanted = changes.changes_for(candidate.kind)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error

    updated = candidates.replace_fields(candidate_id, wanted)
    # A name somebody has actually typed is no longer a stand-in.
    if "name" in wanted:
        updated = candidates.mark_named(candidate_id) or updated

    return _as_import_candidate(graph, updated, candidates.all())


@app.post(
    "/admin/import/candidates/{candidate_id}/approve",
    response_model=CandidateReviewResult,
    summary="Put one candidate into the map",
    responses={
        404: {"description": "No candidate with that id."},
        409: {"description": "Something it depends on has not been approved."},
    },
)
def approve_import_candidate(
    candidate_id: str,
    request: Request,
    graph: CampusGraph = Depends(get_graph),
    candidates: CandidateStore = Depends(get_candidates),
) -> CandidateReviewResult:
    """Approve one candidate, which is the moment it becomes real.

    It goes into the overrides file, so it is live immediately - routable and
    searchable - and appears in /admin/pending awaiting graduation into the
    survey, exactly like a place added by hand.
    """
    candidate = candidates.get(candidate_id)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No candidate {candidate_id!r}.",
        )

    updated = _approve_one(request, graph, candidate, candidates)
    candidates.remove(candidate_id)
    return CandidateReviewResult(
        id=candidate.id,
        target_id=candidate.target_id,
        approved=True,
        node_count=len(updated.nodes),
        edge_count=len(updated.edges),
        remaining=len(candidates.all()),
    )


def _approve_floorplan(request: Request, candidate: Candidate) -> None:
    """Rebuild one floor's plan from the drawing it was read from, and store it.

    Rebuilt rather than carried through the queue: the image is about a
    megabyte, the queue is a JSON file somebody opens to see what is waiting,
    and the build is deterministic, so the plan approved is the plan reviewed.
    """
    sources: ImportSourceStore = request.app.state.import_sources
    floorplans: FloorplanStore = request.app.state.floorplans
    fields = candidate.fields

    try:
        pdf = sources.read(fields["source_id"])
    except ImportSourceError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error

    extractions = read_uploaded_pdf(pdf)
    floor_of = {
        node_id_for(place.name): _building_and_floor(place.name, extraction.floor)
        for extraction in extractions
        if extraction.floor
        for place in extraction.places
    }
    drafts, _ = build_drafts(pdf, extractions, floor_of)

    wanted = (fields["building"], fields["floor"])
    draft = next(
        (d for d in drafts if (d.building, d.floor) == wanted),
        None,
    )
    if draft is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The drawing no longer yields a plan for {' '.join(wanted).strip()}. "
                "Upload it again to see what it says now."
            ),
        )

    # Added, never edited in place. A plan already stored keeps its id and its
    # calibration, so a route drawn a moment ago still resolves; `for_floor`
    # takes the newest, which is what makes this a replacement.
    stored = floorplans.add(
        content=draft.image,
        content_type=draft.content_type,
        building=draft.building,
        floor=draft.floor,
        origin_x_m=draft.origin_x_m,
        origin_y_m=draft.origin_y_m,
        metres_per_pixel=draft.metres_per_pixel,
        note=(
            f"Composed from {draft.made_of} crop(s) of {candidate.source}. "
            f"Scale fitted from {draft.links_used} links, spread "
            f"{draft.spread:.1f}x."
        ),
    )
    logger.info(
        "Stored floorplan %s for %s %s", stored.id, draft.building, draft.floor
    )

    # Places queued before this plan existed had nowhere to be measured
    # against, so they are waiting with no position. The draft worked out
    # where every one of them lands while it was fitting the scale, so the
    # answer is already here - and without this the reviewer would approve a
    # floor of places that never appear on the map they just approved.
    store: CandidateStore = request.app.state.candidates
    placed = 0
    for candidate in store.all():
        if candidate.kind != "node" or candidate.fields.get("x") is not None:
            continue
        at = draft.places.get(candidate.target_id)
        if at is None:
            continue
        store.replace_fields(candidate.id, {"x": at[0], "y": at[1]})
        placed += 1
    if placed:
        logger.info("Gave %s waiting place(s) a position from that plan", placed)


def _approve_one(
    request: Request,
    graph: CampusGraph,
    candidate: Candidate,
    candidates: CandidateStore,
) -> CampusGraph:
    """Write one candidate into the overrides file, or say why it cannot be."""
    if candidate.kind == "floorplan":
        # Nothing routes over a picture, so this touches the floorplan store
        # rather than the overrides file and leaves the graph exactly as it
        # was - which is why it hands back the graph it was given.
        _approve_floorplan(request, candidate)
        return graph

    if candidate.kind == "edge":
        missing = [
            end
            for end in (candidate.fields["from"], candidate.fields["to"])
            if end not in graph.nodes
        ]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{', '.join(missing)} is not on the map yet. Approve the "
                    "place before the link that leads to it."
                ),
            )

    if candidate.kind == "node":
        if candidate.target_id in graph.nodes:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A place with id {candidate.target_id!r} already exists.",
            )
        fields = dict(candidate.fields)
        return _apply_change(request, lambda path: add_node(path, candidate.target_id, fields))

    if candidate.target_id in graph.edges_by_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A link with id {candidate.target_id!r} already exists.",
        )
    fields = dict(candidate.fields)
    return _apply_change(request, lambda path: add_edge(path, candidate.target_id, fields))


@app.post(
    "/admin/import/approve-all",
    response_model=BulkApprovalResult,
    summary="Put every candidate into the map, places before links",
)
def approve_all_import_candidates(
    request: Request,
    graph: CampusGraph = Depends(get_graph),
    candidates: CandidateStore = Depends(get_candidates),
) -> list[CandidateReviewResult]:
    """Approve everything waiting, in an order that can actually be applied.

    Places first: a link cannot be added before the places at its ends exist,
    and asking a reviewer to work that out for themselves would make the
    button useless on any real drawing.
    """
    # Floorplans, then places, then links. Each needs the one before it: a
    # link needs both its places to exist, and a place gets its position from
    # the floor's plan, so approving in any other order leaves work undone
    # that the reviewer would have to notice for themselves.
    waiting = candidates.all()
    ordered = (
        [c for c in waiting if c.kind == "floorplan"]
        + [c for c in waiting if c.kind == "node"]
        + [c for c in waiting if c.kind == "edge"]
    )

    approved: list[CandidateReviewResult] = []
    skipped: list[SkippedCandidate] = []

    for candidate in ordered:
        # The graph changes underneath each approval, so it is re-read rather
        # than captured once: the place approved a moment ago is what makes
        # the link after it approvable at all.
        current = get_graph(request)
        try:
            updated = _approve_one(request, current, candidate, candidates)
        except HTTPException as refusal:
            # One candidate that cannot be applied is not a reason to abandon
            # the rest, and it is certainly not a reason to throw away the
            # report of everything already approved - those changes are on the
            # map by now whatever this response says. It stays in the queue,
            # because nobody has decided anything about it.
            skipped.append(
                SkippedCandidate(
                    id=candidate.id,
                    target_id=candidate.target_id,
                    reason=str(refusal.detail),
                )
            )
            continue

        candidates.remove(candidate.id)
        approved.append(
            CandidateReviewResult(
                id=candidate.id,
                target_id=candidate.target_id,
                approved=True,
                node_count=len(updated.nodes),
                edge_count=len(updated.edges),
                remaining=len(candidates.all()),
            )
        )

    return BulkApprovalResult(
        approved=approved, skipped=skipped, remaining=len(candidates.all())
    )


@app.delete(
    "/admin/import/candidates/{candidate_id}",
    response_model=CandidateReviewResult,
    summary="Throw one candidate away",
    responses={404: {"description": "No candidate with that id."}},
)
def reject_import_candidate(
    candidate_id: str,
    graph: CampusGraph = Depends(get_graph),
    candidates: CandidateStore = Depends(get_candidates),
) -> CandidateReviewResult:
    """Discard a candidate. It was never live, so nothing has to be undone."""
    candidate = candidates.get(candidate_id)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No candidate {candidate_id!r}.",
        )

    candidates.remove(candidate_id)
    return CandidateReviewResult(
        id=candidate.id,
        target_id=candidate.target_id,
        approved=False,
        node_count=len(graph.nodes),
        edge_count=len(graph.edges),
        remaining=len(candidates.all()),
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

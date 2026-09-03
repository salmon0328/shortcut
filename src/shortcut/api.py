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

from shortcut.graph_store import CampusGraph, UnknownNodeError, load_graph
from shortcut.schemas import RouteRequest, RouteResponse
from shortcut.tools.astar import NoRouteFoundError, find_route

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
    app.state.graph = load_graph(CAMPUS_GRAPH_PATH)
    yield


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


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/health", summary="Check that the service is running")
def health() -> dict[str, str]:
    """Cheap liveness check for deployment tools and uptime monitors."""
    return {"status": "ok"}


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
        route = find_route(graph, route_request.origin, route_request.destination)
    except UnknownNodeError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Unknown node id {error.node_id!r}. "
                f"Check the ids in data/campus_graph.json."
            ),
        ) from error
    except NoRouteFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No walkable route from {error.origin!r} to "
                f"{error.destination!r}. Every connecting path may be blocked."
            ),
        ) from error

    return RouteResponse.from_route(route)

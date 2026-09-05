"""The HTTP surface for the agentic layer.

Two endpoints, and one structural decision worth explaining.

**This module imports nothing from :mod:`shortcut.api`.** It would be natural
to reach for ``get_graph`` directly, and it would even appear to work, because
``api`` includes this router at the bottom of the file after everything it
defines exists. But it is a cycle, and it breaks the first time anything
imports ``shortcut.ai.routes`` before ``shortcut.api`` - a test doing exactly
that gets a half-initialised module and an ImportError that points nowhere
near the cause. So the graph dependency is handed in:

    app.include_router(build_ai_router(graph_dependency=get_graph))

Passing the real ``get_graph`` keeps ``app.dependency_overrides[get_graph]``
working for ``/ai/*`` exactly as it does for ``/route``, which is how every
test in this project points the app at a temporary map.

The model client is built lazily rather than during startup. A missing AWS
credential should cost one bad ``/ai/parse`` response, not a server that
refuses to boot and takes the deterministic routing down with it.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status

from shortcut.ai.bedrock import LlmError, LlmPort, build_llm
from shortcut.ai.config import AiSettings, load_settings
from shortcut.ai.parser import parse_request
from shortcut.ai.schemas import AiHealth, ParseRequestBody, ParseResult
from shortcut.graph_store import CampusGraph

__all__ = ["build_ai_router", "get_ai_settings", "get_llm"]


def get_ai_settings(request: Request) -> AiSettings:
    """Which model, which region, and whether we are pretending.

    Resolved once and cached on ``app.state``, the same way the graph and the
    report store are. Reading the environment on every request would let a
    half-edited ``.env`` change behaviour mid-session.
    """
    settings: AiSettings | None = getattr(request.app.state, "ai_settings", None)
    if settings is None:
        settings = load_settings()
        request.app.state.ai_settings = settings
    return settings


def get_llm(
    request: Request, settings: AiSettings = Depends(get_ai_settings)
) -> LlmPort:
    """The model client, built on first use and kept.

    Tests override this rather than assigning to ``app.state``: ``app`` is a
    module-level object and its lifespan does not reset ``ai_llm``, so a mock
    left behind would quietly answer for every later test in the session.
    """
    llm: LlmPort | None = getattr(request.app.state, "ai_llm", None)
    if llm is None:
        llm = build_llm(settings)
        request.app.state.ai_llm = llm
    return llm


def _probe(settings: AiSettings) -> tuple[bool, str]:
    """Can we expect a model call to work, without making one?

    Constructing the client is not enough on its own to tell us anything:
    ``ChatBedrockConverse`` accepts a nonsense model id and no credentials at
    all, and only fails when something is actually sent. So the credentials
    are checked too, which is the failure that actually happens - a fresh
    laptop on demo day has none.

    Still no model call, and no tokens spent. Certainty would cost money, and
    this is the question people want answered fifty times an hour.
    """
    if settings.mock_mode:
        return True, (
            "Running offline against canned answers. Set MOCK_MODE=false in "
            ".env to use Bedrock."
        )

    try:
        build_llm(settings)
    except Exception as error:  # noqa: BLE001 - reported, never raised
        return False, f"The model client could not be built: {error}"

    try:
        import boto3  # noqa: PLC0415 - only needed on the real path

        credentials = boto3.Session(
            **({"profile_name": settings.profile} if settings.profile else {})
        ).get_credentials()
    except Exception as error:  # noqa: BLE001
        return False, f"AWS credentials could not be read: {error}"

    if credentials is None:
        return False, (
            "No AWS credentials found. Run `aws configure sso`, then "
            "`python scripts/check_bedrock.py` to check model access."
        )

    return True, f"Ready, using {settings.model_id} in {settings.region}."


def build_ai_router(
    *, graph_dependency: Callable[..., CampusGraph]
) -> APIRouter:
    """Build the ``/ai`` router against the app's own graph dependency."""
    router = APIRouter(prefix="/ai", tags=["ai"])

    @router.get(
        "/health",
        response_model=AiHealth,
        summary="Report whether the language model is reachable",
    )
    def ai_health(settings: AiSettings = Depends(get_ai_settings)) -> AiHealth:
        """Say what is configured, without spending anything to find out.

        Deliberately makes no model call. A health check that costs tokens is
        one nobody runs often enough to be useful, and it would put the
        network into every test that touches this endpoint.

        Never fails: a broken configuration is something the caller needs
        reported, not something that should read as the service being down.
        """
        available, detail = _probe(settings)
        return AiHealth(
            available=available,
            mock_mode=settings.mock_mode,
            model_id=settings.model_id,
            region=settings.region,
            detail=detail,
        )

    @router.post(
        "/parse",
        response_model=ParseResult,
        summary="Turn a plain-language request into a route request",
        responses={
            503: {"description": "The language model could not be reached."}
        },
    )
    def ai_parse(
        body: ParseRequestBody,
        graph: CampusGraph = Depends(graph_dependency),
        llm: LlmPort = Depends(get_llm),
    ) -> ParseResult:
        """Read a sentence into something the ordinary router can run.

        The result carries a real :class:`~shortcut.schemas.RouteRequest`,
        so the caller posts it to ``/route`` unchanged: the plain-language
        path and the pickers hand the router exactly the same thing, and
        every validation rule already written applies to both.

        When a place is ambiguous - and "the lift" is, once two floors are
        surveyed - no request is built. ``question`` carries what to ask and
        ``alternatives`` carries the two places it could have meant.
        """
        try:
            return parse_request(graph, llm, body.text, body.current)
        except LlmError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"Could not read that request: {error} "
                    "Pick a start and destination from the lists instead."
                ),
            ) from error

    return router

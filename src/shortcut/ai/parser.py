"""Plain language in, a validated RouteRequest out. An extraction step.

This is not an agent and the code says so in its shape: one model call, no
loop, no tools, no decisions. It reads a sentence into fields
(:class:`~shortcut.ai.schemas.ParsedIntent`), hands the place phrases to
:mod:`shortcut.ai.places` to resolve, and builds a
:class:`~shortcut.schemas.RouteRequest` only when both ends came back
unambiguous.

The division of labour is the whole design. The model is good at reading
"LT12 to the CS lab, it's raining, I've got ten minutes" and bad at knowing
that this building has two staircases called Staircase 1. So it never sees a
node id, and the code that does never sees the sentence.
"""

from __future__ import annotations

from shortcut.ai.bedrock import LlmPort
from shortcut.ai.places import PlaceMatch, PlaceResolution, resolve_place
from shortcut.ai.prompts import PARSE_SYSTEM, PARSE_VERSION, parse_user_message
from shortcut.ai.schemas import ParsedIntent, ParseResult, PlaceCandidate, PlaceChoice
from shortcut.ai.usage import TokenUsage
from shortcut.graph_store import CampusGraph
from shortcut.schemas import RouteRequest

__all__ = ["parse_request"]


def _candidate(match: PlaceMatch) -> PlaceCandidate:
    return PlaceCandidate(
        node_id=match.node_id,
        name=match.name,
        building=match.building,
        floor=match.floor,
        score=match.score,
        why=match.why,
    )


def _choice(resolution: PlaceResolution) -> PlaceChoice:
    return PlaceChoice(
        phrase=resolution.phrase,
        resolved=_candidate(resolution.resolved) if resolution.resolved else None,
        alternatives=[_candidate(match) for match in resolution.alternatives],
        ambiguous=resolution.ambiguous,
        reason=resolution.reason,
    )


def _merge(said: bool | None, current: bool) -> bool:
    """Apply what the student said on top of what the controls already show.

    ``None`` means the sentence never raised it, so the existing toggle wins.
    This is why :class:`ParsedIntent` uses three-valued booleans: collapsing
    "did not mention" into ``False`` would have every request silently switch
    off the user's own settings.
    """
    return current if said is None else not said


def parse_request(
    graph: CampusGraph,
    llm: LlmPort,
    text: str,
    current: RouteRequest | None = None,
) -> ParseResult:
    """Read ``text`` into a route request, or into a question worth asking.

    ``current`` is whatever the controls on screen already say. Anything the
    sentence does not mention is taken from there unchanged.
    """
    intent, usage = llm.structured(
        ParsedIntent,
        purpose=PARSE_VERSION,
        system=PARSE_SYSTEM,
        user=parse_user_message(text),
    )
    return build_result(graph, intent, current=current, usage=usage)


def build_result(
    graph: CampusGraph,
    intent: ParsedIntent,
    *,
    current: RouteRequest | None = None,
    usage: TokenUsage | None = None,
) -> ParseResult:
    """The deterministic half, split out so it can be tested without a model.

    Every test of resolution, ambiguity and toggle-merging calls this directly
    with a fixture intent. Only one test needs a model at all.
    """
    origin_resolution = resolve_place(graph, intent.origin_phrase or "")
    destination_resolution = resolve_place(graph, intent.destination_phrase)

    origin = _choice(origin_resolution)
    destination = _choice(destination_resolution)

    question = _first_question(intent, origin_resolution, destination_resolution)
    resolved_both = origin.resolved is not None and destination.resolved is not None

    request: RouteRequest | None = None
    if resolved_both and question is None:
        base = current or RouteRequest(
            origin=origin.resolved.node_id, destination=destination.resolved.node_id
        )
        request = RouteRequest(
            origin=origin.resolved.node_id,
            destination=destination.resolved.node_id,
            preference=intent.preference or base.preference,
            allow_stairs=_merge(intent.avoid_stairs, base.allow_stairs),
            allow_lift=_merge(intent.avoid_lift, base.allow_lift),
            allow_shuttle=_merge(intent.avoid_shuttle, base.allow_shuttle),
            # wants_shelter is a stated requirement, so it may switch the hard
            # filter on. It never switches it off - that is the agent's call,
            # made in the open with a relaxation the user gets told about.
            sheltered_only=base.sheltered_only or bool(intent.wants_shelter),
        )

    return ParseResult(
        request=request,
        origin=origin,
        destination=destination,
        needs_clarification=request is None,
        question=question,
        max_minutes=intent.max_minutes,
        notes=intent.notes,
        usage=usage or TokenUsage.empty(),
    )


def _first_question(
    intent: ParsedIntent,
    origin: PlaceResolution,
    destination: PlaceResolution,
) -> str | None:
    """One question at a time, destination first.

    Asking two things at once reads as an interrogation, and the destination
    is the half a student is more likely to have been vague about.
    """
    if destination.question:
        return destination.question
    if destination.resolved is None:
        return f"I don't know anywhere called {destination.phrase!r}. Where do you mean?"
    if intent.origin_phrase is None:
        return "Where are you starting from?"
    if origin.question:
        return origin.question
    if origin.resolved is None:
        return f"I don't know anywhere called {origin.phrase!r}. Where are you starting from?"
    return None

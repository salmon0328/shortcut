"""Judging reported problems: the one component here that is really an agent.

The parser reads a sentence and stops. This plans, acts on the map, and moves
its own bar while it works - so it is held to a different standard, and the
shape of the code is where that standard lives.

**What it does, in order.** Reads every submission about one problem and rates
each (one model call). Adds the ratings up. Asks :mod:`shortcut.ai.impact`
what closing the place would actually cost - a tool, arithmetic, no model.
Raises its own threshold if that answer is bad enough. Then approves, rejects,
or hands the whole thing to a person.

**Three rules it is built around.**

*The model rates, the code decides.* Weights come back from the model; the
comparison against :data:`~shortcut.ai.config.AUTO_MERGE_THRESHOLD_BLOCKING`
happens here, in Python, against a number the model never sees. A model asked
to output "approve" will eventually be talked into outputting "approve", and
the report notes are attacker-supplied text typed by strangers.

*Closing something costs more than warning about it.* A wrongly-closed
corridor sends somebody the long way round a building in the rain; a wrongly
flagged busy lobby costs them nothing. So blocking conditions need twice the
corroboration advisory ones do, which is the entire content of those two
constants.

*Escalating is a real outcome, not a failure.* Anything that would strand a
place goes to a person no matter how many people reported it, because no
number of confident strangers should be able to punch a hole in the map. The
agent is a filter on an administrator's queue, not a replacement for one.
"""

from __future__ import annotations

import logging

from shortcut.ai.bedrock import LlmError, LlmPort
from shortcut.ai.config import (
    AUTO_MERGE_THRESHOLD_ADVISORY,
    AUTO_MERGE_THRESHOLD_BLOCKING,
    MAX_GATE_RETRIES,
)
from shortcut.ai.impact import Impact, assess
from shortcut.ai.prompts import VERIFY_SYSTEM, VERIFY_VERSION, verify_user_message
from shortcut.ai.schemas import ReportImpact, ReportReading, ReportVerdict
from shortcut.ai.usage import TokenUsage
from shortcut.graph_store import CampusGraph
from shortcut.report_store import Report, ReportGroup

__all__ = ["verify_group"]

logger = logging.getLogger("shortcut.ai")

#: How much longer the way round has to get before a closure stops counting as
#: an inconvenience. Two minutes is roughly the difference between stepping
#: through the next door along and walking round the outside of a building.
LONG_DETOUR_SECONDS = 120.0


def _reading_of(
    llm: LlmPort,
    group: ReportGroup,
    reports: list[Report],
    place: str,
) -> tuple[ReportReading, TokenUsage]:
    """Ask the model what the submissions are worth, retrying a bad answer.

    A model that returns something unparseable is having a bad moment, not
    making a judgement, so it gets another go. It does not get an unlimited
    number of them: past :data:`MAX_GATE_RETRIES` the honest conclusion is
    that this is not working, and a person should look.
    """
    message = verify_user_message(
        place=place,
        target_kind=group.target_kind,
        condition=group.condition,
        notes=[(report.id, report.notes) for report in reports],
    )

    last: LlmError | None = None
    for attempt in range(MAX_GATE_RETRIES + 1):
        try:
            return llm.structured(
                ReportReading,
                purpose=VERIFY_VERSION,
                system=VERIFY_SYSTEM,
                user=message,
            )
        except LlmError as error:
            last = error
            logger.warning(
                "verify %s: attempt %d/%d gave nothing usable: %s",
                group.key,
                attempt + 1,
                MAX_GATE_RETRIES + 1,
                error,
            )
    raise last if last else LlmError(f"verify {group.key}: no answer and no error")


def _total_weight(reading: ReportReading, reports: list[Report]) -> float:
    """Add up the weights, counting each submission at most once.

    Keyed by report id rather than summed as returned: a model that repeats an
    id - or invents one - must not be able to inflate the total past what the
    submissions can support. Anything it failed to rate counts as zero, which
    is the safe direction: it holds the report back rather than pushing it
    through on a rating nobody gave.
    """
    rated = {weight.report_id: weight.weight for weight in reading.weights}
    return round(sum(rated.get(report.id, 0.0) for report in reports), 3)


def _threshold_for(group: ReportGroup, impact: Impact) -> tuple[float, float, str]:
    """The bar this report has to clear, and why it is where it is.

    Returns ``(threshold, base, why it was raised)``. Isolation is not handled
    here: nothing clears that bar, and expressing "impossible" as a number
    would leave the verdict reporting a threshold that no arithmetic produced.
    :func:`_decide` refuses it outright instead.
    """
    base = (
        AUTO_MERGE_THRESHOLD_BLOCKING
        if group.blocks_routes
        else AUTO_MERGE_THRESHOLD_ADVISORY
    )

    # A long way round is not a hole in the map, but it is the difference
    # between an inconvenience and a trek, so it is worth one more voice.
    if impact.detour_seconds is not None and impact.detour_seconds >= LONG_DETOUR_SECONDS:
        return (
            base + 1.0,
            base,
            f"the way round costs about {round(impact.detour_seconds)}s more",
        )

    return base, base, ""


def verify_group(
    graph: CampusGraph,
    llm: LlmPort,
    group: ReportGroup,
    reports: list[Report],
    place: str,
) -> ReportVerdict:
    """Judge one group of reports about one problem.

    ``reports`` are the submissions in ``group``, and ``place`` is its
    readable name - both passed in rather than looked up, so this stays a
    function of its arguments and the tests do not need a store.

    Decides but does not act: applying an approval writes overrides and
    rebuilds the graph, which belongs to the caller. What comes back says what
    should happen and every number behind it.
    """
    impact = assess(
        graph,
        target_kind=group.target_kind,
        target_id=group.target_id,
        condition=group.condition,
    )
    reading, usage = _reading_of(llm, group, reports, place)
    weight = _total_weight(reading, reports)
    threshold, base, raised = _threshold_for(group, impact)

    action, why = _decide(reading, impact, weight, threshold, raised)

    logger.info(
        "verify key=%s action=%s weight=%.2f threshold=%s impact=%s",
        group.key,
        action,
        weight,
        threshold,
        impact.describes,
    )

    return ReportVerdict(
        key=group.key,
        action=action,
        weight=weight,
        threshold=threshold,
        base_threshold=base,
        raised_because=raised,
        impact=ReportImpact(
            blocks_routes=impact.blocks_routes,
            cut_off=list(impact.cut_off),
            detour_seconds=impact.detour_seconds,
            describes=impact.describes,
        ),
        reading=reading,
        why=why,
        usage=usage,
    )


def _decide(
    reading: ReportReading,
    impact: Impact,
    weight: float,
    threshold: float,
    raised: str,
) -> tuple[str, str]:
    """Approve, reject, or hand it over - and say which, in a person's words."""
    # Nothing anybody wrote is worth anything. Rejected outright rather than
    # escalated: an administrator's queue is a scarce thing, and four notes
    # reading "test" and "lol" do not need a human to adjudicate them.
    if weight <= 0:
        return "rejected", (
            "Nothing in these submissions describes a real problem worth "
            "acting on."
        )

    # Checked before the arithmetic, because no amount of corroboration should
    # be able to punch a hole in the map. This is the threshold the agent
    # cannot move, and the one that makes the rest of it safe to automate.
    if impact.isolates:
        return "escalated", (
            f"Held for a person: approving this would leave "
            f"{len(impact.cut_off)} place(s) with no way in at all "
            f"({', '.join(impact.cut_off)}), which is not something "
            "corroboration can settle."
        )

    # Filed as the wrong kind of problem. Rejecting would throw away something
    # that may well be true, so it goes to a person, who can re-file it.
    if not reading.describes_condition:
        return "escalated", (
            "The notes describe something other than the problem they were "
            "filed under, so this needs a person to re-file rather than a "
            "yes or no."
        )

    if reading.contradiction:
        return "escalated", (
            f"The submissions disagree with each other: {reading.contradiction}"
        )

    if weight >= threshold:
        return "approved", (
            f"{weight:g} of corroboration against a bar of {threshold:g}, and "
            "nothing about the map argues against it."
        )

    if raised:
        return "escalated", (
            f"{weight:g} of corroboration, short of the {threshold:g} needed "
            f"here because {raised}."
        )

    # Deliberately not rejected. Too few people have said anything yet, which
    # is a reason to wait rather than to decide the problem is not real -
    # rejecting would close a queue entry that tomorrow's reporter would have
    # completed.
    return "escalated", (
        f"Only {weight:g} of corroboration so far, against {threshold:g}. "
        "Waiting for more, or for a person."
    )

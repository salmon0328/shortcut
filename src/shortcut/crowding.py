"""How busy somewhere is right now, and what that does to a route.

A blocked corridor and a busy one are different kinds of fact, and this module
exists because the difference matters.

*Blocked* is a change to the map. It needs corroborating, a moderator can
overrule it, and once agreed it is written down - see :mod:`shortcut.overrides`.
*Busy* is a change to the hour. The lift outside the lecture theatre has a
twenty second wait at ten past nine and three minutes at ten to, and neither
number is more true than the other. Writing that into the map would be
recording the weather in the atlas.

So crowding is never stored. It is computed here, at routing time, from the
reports people have filed recently, and it reaches the search as extra seconds
on the affected edges. Three things fall out of that choice:

* **Reports expire on their own.** Anything older than :data:`CROWD_TTL_MINUTES`
  is simply not counted. Nothing has to be cleaned up, and a busy lift quietly
  stops being busy once the rush is over.
* **The surveyed map never changes**, so it stays the reviewable, measured
  thing it is meant to be.
* **One report is enough to have an effect**, unlike a blocking report which
  waits for corroboration. Wrongly closing a corridor sends people the long
  way round for nothing; wrongly calling a lobby busy costs a few seconds and
  undoes itself within the hour. The stakes are different, so the thresholds
  are too.

The numbers below come from the Hive's B5-B4 lift, which was timed at about
twenty seconds off-peak and about three minutes at its worst.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shortcut.graph_store import CampusGraph
from shortcut.report_store import Report
from shortcut.tools.astar import CostFunction

__all__ = [
    "CROWD_TTL_MINUTES",
    "CROWD_WAIT_CAP_SECONDS",
    "CROWD_WAIT_STEP_SECONDS",
    "crowd_waits",
    "with_crowding",
]


#: How much slower one person's report says somewhere has become. Reports of
#: the same place stack, so a place several people have flagged is treated as
#: worse than one only a single person noticed.
CROWD_WAIT_STEP_SECONDS = 40.0

#: The most crowding can ever add. Four reports reach it, which takes the
#: surveyed lift from its measured twenty second off-peak wait to the three
#: minutes it was timed at during the rush. Past that we stop believing the
#: reports rather than pretending a lift takes ten minutes: crowding makes a
#: way slow, never impassable, and the cap is what guarantees that.
CROWD_WAIT_CAP_SECONDS = 160.0

#: How long a report speaks for. Long enough to cover a change of lectures,
#: short enough that the lunch rush does not still be happening at four.
CROWD_TTL_MINUTES = 45


def _parse_time(stamp: str) -> datetime | None:
    """Read an ISO timestamp, treating anything unreadable as expired.

    A malformed row should never crash a route request, and never silently
    slow one down either, so it is ignored rather than guessed at.
    """
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def crowd_waits(
    graph: CampusGraph,
    reports: list[Report],
    now: datetime | None = None,
) -> dict[str, float]:
    """How many extra seconds each edge is carrying right now.

    A report against an edge slows that edge. A report against a *node* slows
    every edge touching it, which mirrors how a blocked node closes everything
    around it (see :func:`shortcut.overrides.apply_overrides`) - you cannot
    walk through a packed lobby without being in it.

    Only pending and approved reports count. A moderator who rejected one has
    said it was not true, and it should stop affecting routes immediately.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(minutes=CROWD_TTL_MINUTES)

    waits: dict[str, float] = {}
    for report in reports:
        if report.condition != "crowded" or report.status == "rejected":
            continue
        submitted = _parse_time(report.submitted_at)
        if submitted is None or submitted < cutoff:
            continue

        if report.target_kind == "edge":
            affected = [report.target_id] if report.target_id in graph.edges_by_id else []
        else:
            affected = [
                edge.id
                for edge in graph.edges_from(report.target_id, include_blocked=True)
            ] if graph.has_node(report.target_id) else []

        for edge_id in affected:
            waits[edge_id] = min(
                waits.get(edge_id, 0.0) + CROWD_WAIT_STEP_SECONDS,
                CROWD_WAIT_CAP_SECONDS,
            )

    return waits


def with_crowding(cost: CostFunction, waits: dict[str, float]) -> CostFunction:
    """Wrap a cost function so busy ways score worse.

    Returns ``cost`` unchanged when nothing is crowded, so the common case
    pays nothing for this feature - not even a per-edge function call.
    """
    if not waits:
        return cost

    def crowded_cost(edge) -> float:
        return cost(edge) + waits.get(edge.id, 0.0)

    return crowded_cost

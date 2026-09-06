"""Turning an uploaded drawing into changes somebody can look at first.

:mod:`shortcut.nodemap` reads shapes and words out of a PDF. This decides
which of them the map does not already have, and describes each one well
enough that a reviewer can accept it, correct it, or throw it away.

Nothing here writes to the map. That matters: a drawing is somebody's sketch
of a building, not a survey of one, and the difference only shows up when a
person who knows the place looks at what was read.

**What it refuses to decide.** The drawing is reliable about geometry and
unreliable about meaning, so the split runs along that line:

* *read from the drawing* - which places exist, where they sit, what joins
  what, how many seconds each walk takes
* *left for the reviewer* - what a place is called, what kind of place it is,
  whether a corridor is rained on

Anything in the second group arrives as a stated default with the drawing's
own evidence attached, never as a silent guess. Where the drawing wrote
"stairs" beside a line, that word is carried onto the candidate and the
stairs flag is set from it - the older importer dropped those words for any
line it could otherwise complete, which quietly turned marked staircases
into flat corridors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shortcut.graph_store import CampusGraph
from shortcut.nodemap import Extraction, metres_for, node_id_for

__all__ = [
    "EdgeCandidate",
    "ImportReading",
    "NodeCandidate",
    "PlanCalibration",
    "read_candidates",
]


@dataclass(frozen=True)
class PlanCalibration:
    """What turns a place's position on a drawing into a position on the map.

    :mod:`shortcut.nodemap` reads a position as a *fraction* of the plan it is
    drawn on, which is the only form that survives the same plan being scanned
    at two resolutions. The rest of the app works in **metres** - the map
    panel, ``Floorplan.to_pixels`` and every position the earlier survey
    recorded - so a fraction has to be converted before it is stored, using
    the scale somebody already fitted for that floor.

    Getting this wrong is not visible in the data: a fraction stored where
    metres are expected is a perfectly valid number that draws every place in
    the top-left corner of its floorplan.
    """

    origin_x_m: float
    origin_y_m: float
    metres_per_pixel: float
    width_px: int
    height_px: int

    def to_metres(self, fraction: tuple[float, float]) -> tuple[float, float]:
        """A fraction of the plan, as metres on the map."""
        across, down = fraction
        return (
            round(self.origin_x_m + across * self.width_px * self.metres_per_pixel, 2),
            round(self.origin_y_m + down * self.height_px * self.metres_per_pixel, 2),
        )


#: Words a drawing writes on a line to say how it is walked. Read as a
#: default the reviewer can overrule, not as the last word.
_STAIRS_MARKS = frozenset({"stairs", "staircase", "wheelchair"})
_LIFT_MARKS = frozenset({"lift", "escalator"})


@dataclass
class NodeCandidate:
    """A place the drawing has and the map does not."""

    node_id: str
    name: str
    building: str
    floor: str
    type: str = "junction"
    #: Where it sits on its floorplan, as a fraction of the plan image. None
    #: when the drawing placed it somewhere no plan covers.
    x: float | None = None
    y: float | None = None
    #: Which page it was read from, so a reviewer can go and look.
    source: str = ""
    #: True when the drawing only ever called it "Hive-B3-C", so `name` is a
    #: stand-in the reviewer is expected to replace.
    name_is_a_stand_in: bool = True

    def as_fields(self) -> dict[str, Any]:
        """The shape :func:`shortcut.overrides.add_node` wants."""
        fields = {
            "name": self.name,
            "building": self.building,
            "floor": self.floor,
            "type": self.type,
        }
        if self.x is not None and self.y is not None:
            fields["x"] = self.x
            fields["y"] = self.y
        return fields


@dataclass
class EdgeCandidate:
    """A walk the drawing has and the map does not."""

    edge_id: str
    from_id: str
    to_id: str
    walk_seconds: float
    distance_m: float
    #: Indoors unless somebody says otherwise. The drawing does not record
    #: weather, so this is the reviewer's call and is flagged as such.
    covered: bool = True
    stairs: bool = False
    lift: bool = False
    #: What the drawing wrote on this line, if anything - "stairs",
    #: "Wheelchair". Kept as written so a reviewer can see why the flags
    #: above were set, rather than being asked to trust them.
    marks: tuple[str, ...] = ()
    source: str = ""
    #: True when both ends are places the map already has, and it has no such
    #: link between them. That is not new data, it is a contradiction, and it
    #: has two very different explanations: either the drawing is newer than
    #: the survey and the walk was missed, or it is older and its lettering
    #: means different places by the same names. A reviewer who is only shown
    #: "add this link?" cannot tell those apart, so the question has to be
    #: asked differently - which is what this flag is for.
    disagrees_with_survey: bool = False

    def as_fields(self) -> dict[str, Any]:
        """The shape :func:`shortcut.overrides.add_edge` wants."""
        return {
            "from": self.from_id,
            "to": self.to_id,
            "distance_m": self.distance_m,
            "walk_seconds": self.walk_seconds,
            "covered": self.covered,
            "stairs": self.stairs,
            "lift": self.lift,
            "blocked": False,
        }


@dataclass
class ImportReading:
    """Everything one upload yielded, including what it could not settle."""

    nodes: list[NodeCandidate] = field(default_factory=list)
    edges: list[EdgeCandidate] = field(default_factory=list)
    #: Lines the drawing did not settle - an end that touches no place, a
    #: time written "?s", a time that could belong to either of two lines.
    #: Reported rather than guessed at, in the words of the drawing.
    unsettled: list[str] = field(default_factory=list)
    #: Things the map already has, counted so an upload that changes nothing
    #: says so instead of looking like it failed.
    already_known_nodes: int = 0
    already_known_edges: int = 0

    def is_empty(self) -> bool:
        return not self.nodes and not self.edges


def _flags_from(marks: tuple[str, ...]) -> tuple[bool, bool]:
    """Whether a line's words make it stairs, a lift, or neither."""
    lowered = {mark.lower() for mark in marks}
    return bool(lowered & _STAIRS_MARKS), bool(lowered & _LIFT_MARKS)


def _building_and_floor(drawn_name: str, page_floor: str = "") -> tuple[str, str]:
    """Read a place's building and floor from its label, then from its page.

    ``Hive-B3-D`` says both itself. Plenty do not: the walkway, the canteen
    and the four unnamed Hive doors are labelled ``Hive-SS-Walkway-A``,
    ``SS-Canteen-B``, ``Hive-A``. For those the page is the authority, and
    it is a real one - a page headed "Hive + SS B4" states the floor in its
    own title, which is different in kind from the older importer taking it
    from a hardcoded page number.

    Still empty when neither says. Some places genuinely have no floor: the
    walkway between the buildings is outdoors at road level, and inventing a
    floor for it would be filling a field rather than recording a fact.
    """
    parts = drawn_name.split("-")
    building = "Hive-SS" if drawn_name.startswith("Hive-SS-Walkway") else parts[0]

    if len(parts) >= 3 and parts[1].startswith("B") and parts[1][1:].isdigit():
        return building, parts[1]
    return building, page_floor


def _stand_in_name(drawn_name: str) -> str:
    """The label with its dashes removed, and nothing cleverer.

    "Hive B3 C" is obviously nobody's name for anywhere, which is the point:
    a prettier guess would read like a surveyed name and survive review.
    """
    return drawn_name.replace("-", " ")


def read_candidates(
    graph: CampusGraph,
    extractions: tuple[Extraction, ...],
    *,
    filename: str = "",
    calibrations: dict[tuple[str, str], PlanCalibration] | None = None,
) -> ImportReading:
    """Everything in ``extractions`` that ``graph`` does not already have.

    Both ends of a link must be a place that exists or is being added in the
    same reading, so approving the whole set can never leave an edge pointing
    at nothing.

    ``calibrations`` says how to turn a position on a drawing into metres, per
    building and floor. A place on a floor with no calibrated plan gets **no
    position at all** rather than a raw fraction: an unplaced place is a gap
    the map can report, while a fraction stored as metres is a place drawn
    confidently in the wrong spot.
    """
    reading = ImportReading()
    where = f"{filename} " if filename else ""
    calibrations = calibrations or {}

    # -- places ------------------------------------------------------------
    seen: dict[str, NodeCandidate] = {}
    for extraction in extractions:
        for place in extraction.places:
            node_id = node_id_for(place.name)
            if node_id in graph.nodes:
                reading.already_known_nodes += 1
                continue
            if node_id in seen:
                continue  # the same place drawn on two plans

            building, floor = _building_and_floor(place.name, extraction.floor)
            fraction = place.fraction
            calibration = calibrations.get((building, floor))
            at = (
                calibration.to_metres(fraction)
                if fraction is not None and calibration is not None
                else None
            )
            seen[node_id] = NodeCandidate(
                node_id=node_id,
                name=_stand_in_name(place.name),
                building=building,
                floor=floor,
                x=at[0] if at else None,
                y=at[1] if at else None,
                source=f"{where}page {extraction.page + 1}",
            )

    reading.nodes = sorted(seen.values(), key=lambda node: node.node_id)

    # -- links -------------------------------------------------------------
    will_exist = set(graph.nodes) | set(seen)
    known_pairs = {frozenset((edge.from_id, edge.to_id)) for edge in graph.edges}
    added_pairs: set[frozenset[str]] = set()

    for extraction in extractions:
        page = f"{where}page {extraction.page + 1}"
        for line in extraction.lines:
            if not line.is_complete:
                ends = " -- ".join(str(end or "?") for end in (line.from_place, line.to_place))
                times = ", ".join(line.times) or "no time"
                marked = f" (marked {', '.join(line.notes)})" if line.notes else ""
                reading.unsettled.append(f"{page}: {ends}, {times}{marked}")
                continue

            first, second = sorted(
                (node_id_for(line.from_place), node_id_for(line.to_place))
            )
            pair = frozenset((first, second))

            if pair in known_pairs:
                reading.already_known_edges += 1
                continue
            if pair in added_pairs:
                continue  # a crossing drawn on both buildings' plans
            # Cannot happen for a complete line, since both ends touched a
            # drawn square - but an edge to a place that does not exist would
            # make the graph unloadable, so it is checked rather than assumed.
            if not all(end in will_exist for end in pair):
                reading.unsettled.append(f"{page}: {first} -- {second}, one end is not a place")
                continue

            stairs, lift = _flags_from(line.notes)
            added_pairs.add(pair)
            reading.edges.append(
                EdgeCandidate(
                    edge_id=f"{first}--{second}",
                    from_id=first,
                    to_id=second,
                    walk_seconds=line.seconds,
                    distance_m=metres_for(line.seconds),
                    stairs=stairs,
                    lift=lift,
                    marks=tuple(line.notes),
                    source=page,
                    disagrees_with_survey=all(end in graph.nodes for end in pair),
                )
            )

    reading.edges.sort(key=lambda edge: edge.edge_id)
    return reading

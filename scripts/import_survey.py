"""Turn the drawn node map into the survey file, without retyping it.

    .venv/bin/python scripts/import_survey.py            # say what would change
    .venv/bin/python scripts/import_survey.py --write    # change it

Run it, read ``data/survey_review.md``, read ``git diff data/campus_graph.json``,
commit if you agree. It never runs inside the server and never touches live
state; the survey's review has always been a git diff and stays one.

Three rules hold, and the first is the one that makes this safe to re-run:

**Nothing already in the survey is ever changed.** Every committed place keeps
its name, its type and its times. If the drawing disagrees with what is
committed - and on one edge it does - the disagreement is *reported*, not
applied. Somebody walked the building to produce those numbers, and a script
that reads a PDF does not get to overrule them.

**Only positions are added to places that already exist**, because the survey
has none and that is why the map draws nothing today.

**Anything the drawing does not settle is a question, not a default.** The
cross-floor connectors run off the edge of the slide toward a label rather
than ending on a place, and a few times were left as ``?s``. Those go to
``data/survey_review.md`` for a person, and the importer writes no edge for
them. A guessed staircase would look exactly like a surveyed one.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nodemap import (  # noqa: E402
    Extraction,
    Place,
    metres_for,
    node_id_for,
    read_node_map,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE_MAP = PROJECT_ROOT / "data" / "survey_sources" / "hive_node_map.pdf"
SURVEY = PROJECT_ROOT / "data" / "campus_graph.json"
PLACES_CSV = PROJECT_ROOT / "data" / "survey_places.csv"
LINKS_CSV = PROJECT_ROOT / "data" / "survey_links.csv"
REVIEW = PROJECT_ROOT / "data" / "survey_review.md"

#: Which floor each surveyed page is drawing. The page says so in its title,
#: but reading a title to decide where a corridor is would be a silly way to
#: get it wrong, so it is written down here instead.
PAGE_FLOOR = {6: "B5", 7: "B4", 8: "B3"}

#: The linkway between the two buildings is not in either of them. Giving it
#: its own building keeps a Hive floorplan from being served for it, and keeps
#: "which floor am I on" answerable for every place.
WALKWAY_BUILDING = "Hive-SS"

#: How many links a floor needs before its fitted scale is believed. Walking
#: times and straight lines on a plan never agree exactly - a corridor that
#: bends is longer walked than drawn - so the scale is a median over many
#: links, and a median of two is just the mean of two guesses.
MIN_LINKS_FOR_A_SCALE = 3


@dataclass
class NewPlace:
    node_id: str
    name: str
    building: str
    floor: str
    type: str
    x: float | None
    y: float | None


def building_and_floor(drawn_name: str, page: int) -> tuple[str, str]:
    """Where a place is, from what it is called and which page drew it.

    ``Hive-B3-D`` says both outright. ``SS-Canteen-A`` says only the building,
    and takes its floor from the page it was drawn on, which is the same thing
    the surveyor meant by drawing it there.
    """
    parts = drawn_name.split("-")
    floor = PAGE_FLOOR[page]
    if drawn_name.startswith("Hive-SS-Walkway"):
        return WALKWAY_BUILDING, floor
    if len(parts) >= 3 and parts[1].startswith("B") and parts[1][1:].isdigit():
        return parts[0], parts[1]
    return parts[0], floor


def readable(drawn_name: str) -> str:
    """A stand-in name, obviously a stand-in, for somebody to replace.

    Deliberately just the code with the dashes taken out. A prettier guess -
    "Staircase A" - would read like a surveyed name and survive review, and
    the survey would end up full of names nobody chose.
    """
    return drawn_name.replace("-", " ")


def home_plans(extractions: tuple[Extraction, ...]) -> dict[str, Place]:
    """Where each place is drawn, for the places drawn in exactly one spot.

    A link that crosses between two buildings is drawn on both their plans, so
    its end appears twice - once truly and once as a marker showing where the
    crossing lands. There is no way to tell those apart from the geometry, so
    a place drawn twice gets no position and is reported instead.
    """
    seen: dict[str, list[Place]] = defaultdict(list)
    for extraction in extractions:
        for place in extraction.places:
            if place.plan is not None:
                seen[place.name].append(place)
    return {name: found[0] for name, found in seen.items() if len(found) == 1}


def fit_positions(
    positions: dict[str, Place],
    floor_of: dict[str, tuple[str, str]],
    pairs: dict[frozenset[str], int],
    will_exist: set[str],
) -> tuple[dict[str, tuple[float, float]], dict[tuple[str, str], float], list[str]]:
    """Give every place a position in metres, one scale per floor.

    A place is drawn at a fraction of the way across its floorplan, which is
    true of that plan at any resolution. Turning that into metres needs one
    number per floor, and every link on the floor is evidence for it: the link
    is so many plan-widths long, and the survey says it is so many metres. The
    median of what all of them imply is the scale, and the spread is reported,
    because a floor whose links disagree is a floor that was traced wrong and
    the number saying so is worth more than the scale.
    """
    # Grouped by the plan drawn on, not only by the floor. One floor of SS is
    # drawn as two separate images of two wings, and a fraction of the way
    # across one of them means nothing measured against the other.
    by_plan: dict[tuple[str, str, int], list[str]] = defaultdict(list)
    for name, place in positions.items():
        node_id = node_id_for(name)
        if node_id in will_exist and node_id in floor_of and place.plan is not None:
            building, floor = floor_of[node_id]
            by_plan[(building, floor, place.plan.xref)].append(node_id)

    coordinates: dict[str, tuple[float, float]] = {}
    scales: dict[tuple[str, str], float] = {}
    notes: list[str] = []

    for (building, floor_name, _), node_ids in sorted(by_plan.items()):
        floor = (building, floor_name)
        here = set(node_ids)
        plan = positions[node_ids[0].replace("_", "-")].plan
        assert plan is not None

        def pixels(node_id: str) -> tuple[float, float]:
            fraction = positions[node_id.replace("_", "-")].fraction
            assert fraction is not None
            return (fraction[0] * plan.width, fraction[1] * plan.height)

        ratios: list[float] = []
        for pair, seconds in pairs.items():
            if not pair <= here:
                continue
            first, second = sorted(pair)
            (x1, y1), (x2, y2) = pixels(first), pixels(second)
            length = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5
            if length > 0:
                ratios.append(metres_for(seconds) / length)

        # A scale is only as good as the number of links that vote on it. One
        # or two links can agree perfectly and still both be wrong, and a
        # position that is confidently in the wrong room is worse than no
        # position at all - the map draws it, and nobody thinks to check.
        if len(ratios) < MIN_LINKS_FOR_A_SCALE:
            notes.append(
                f"{floor[0]} {floor[1]}: only {len(ratios)} link(s) join two placed "
                f"places, too few to trust a scale, so these places get no position."
            )
            continue

        # The smallest ratio, not the average of them.
        #
        # A* is only guaranteed to return the shortest route while its
        # straight-line estimate never exceeds the real walk. Take the median
        # and half the links come out longer on the plan than they are in the
        # building, the estimate overshoots on those, and the search starts
        # quietly returning routes that are not the shortest - checked, and it
        # did: 76 of 1560 trips got worse. The smallest ratio is the largest
        # scale at which every link stays within its surveyed length, so the
        # estimate is always a little short and never once too long.
        #
        # The cost is that the plan reads smaller than the building really is,
        # which changes nothing anybody sees: the map divides the scale back
        # out to place a pin, so it cancels, and the only other reader is the
        # estimate that wanted the lower bound in the first place.
        scale = min(ratios)
        spread = max(ratios) / scale
        scales[floor] = scale
        notes.append(
            f"{floor[0]} {floor[1]}: {scale:.6f} m/pixel from {len(ratios)} links. "
            f"The most direct link is {spread:.1f}x tighter than the least, which is "
            f"how much corridors bend on this floor."
        )
        for node_id in node_ids:
            x, y = pixels(node_id)
            coordinates[node_id] = (round(x * scale, 2), round(y * scale, 2))

    return coordinates, scales, notes


def unreachable_groups(
    nodes: set[str], pairs: set[frozenset[str]]
) -> list[set[str]]:
    """The graph split into groups that cannot be walked between, largest first.

    Worth reporting loudly. A survey that grows a floor without the stairs
    that reach it looks complete - more places, more links - and answers "no
    route" for every trip anybody would actually make to it.
    """
    neighbours: dict[str, set[str]] = {node: set() for node in nodes}
    for pair in pairs:
        first, second = sorted(pair)
        if first in neighbours and second in neighbours:
            neighbours[first].add(second)
            neighbours[second].add(first)

    groups: list[set[str]] = []
    unvisited = set(nodes)
    while unvisited:
        group: set[str] = set()
        queue = [unvisited.pop()]
        while queue:
            node = queue.pop()
            group.add(node)
            for other in neighbours[node] - group:
                unvisited.discard(other)
                queue.append(other)
        groups.append(group)
    return sorted(groups, key=len, reverse=True)


def read_confirmed_links() -> list[dict[str, str]]:
    """Links a person settled after reading the review file.

    The drawing leaves some links open - a connector drawn off the edge of
    the page, a time written as ``?s`` - and this script will not close them.
    Somebody who knows the building does, once, here, and it is then applied
    every run like anything else. The alternative is a decision living in a
    commit message, which is a decision nobody can re-apply.
    """
    if not LINKS_CSV.exists():
        return []
    with LINKS_CSV.open(encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row.get("from")]


def load_survey() -> dict:
    return json.loads(SURVEY.read_text(encoding="utf-8"))


def read_places_csv() -> dict[str, dict[str, str]]:
    if not PLACES_CSV.exists():
        return {}
    with PLACES_CSV.open(encoding="utf-8") as handle:
        return {row["id"]: row for row in csv.DictReader(handle) if row.get("id")}


def write_places_csv(rows: list[NewPlace]) -> None:
    """Write the places that still need a human name, for editing in place."""
    existing = read_places_csv()
    with PLACES_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, ["id", "name", "building", "floor", "type"])
        writer.writeheader()
        for row in sorted(rows, key=lambda place: place.node_id):
            was = existing.get(row.node_id, {})
            writer.writerow(
                {
                    "id": row.node_id,
                    "name": was.get("name") or row.name,
                    "building": was.get("building") or row.building,
                    "floor": was.get("floor") or row.floor,
                    "type": was.get("type") or row.type,
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="actually change data/campus_graph.json"
    )
    parser.add_argument("--node-map", type=Path, default=NODE_MAP)
    arguments = parser.parse_args()

    extractions = read_node_map(arguments.node_map)
    survey = load_survey()
    known_nodes = {node["id"]: node for node in survey["nodes"]}
    known_pairs = {
        frozenset((edge["from"], edge["to"])): edge for edge in survey["edges"]
    }
    csv_rows = read_places_csv()
    positions = home_plans(extractions)

    # -- places ------------------------------------------------------------

    drawn: dict[str, tuple[str, int]] = {}
    for extraction in extractions:
        for place in extraction.places:
            drawn.setdefault(place.name, (place.name, extraction.page))

    new_places: list[NewPlace] = []
    for drawn_name, (_, page) in sorted(drawn.items()):
        node_id = node_id_for(drawn_name)
        if node_id in known_nodes:
            continue
        building, floor = building_and_floor(drawn_name, page)
        row = csv_rows.get(node_id, {})
        new_places.append(
            NewPlace(
                node_id=node_id,
                name=row.get("name") or readable(drawn_name),
                building=row.get("building") or building,
                floor=row.get("floor") or floor,
                type=row.get("type") or "junction",
                x=None,
                y=None,
            )
        )

    # -- links -------------------------------------------------------------

    complete: dict[frozenset[str], int] = {}
    questions: list[str] = []
    disagreements: list[str] = []

    for extraction in extractions:
        for line in extraction.lines:
            if line.is_complete:
                pair = frozenset((node_id_for(line.from_place), node_id_for(line.to_place)))
                committed = known_pairs.get(pair)
                if committed is not None:
                    if committed["walk_seconds"] != line.seconds:
                        disagreements.append(
                            f"`{committed['id']}` is {committed['walk_seconds']}s in the "
                            f"survey and {line.seconds}s on page {line.page + 1}. "
                            f"The survey wins; nothing was changed."
                        )
                    continue
                # The same crossing is drawn on both buildings' plans, so it
                # arrives twice. Keeping the first is enough; they agree.
                complete.setdefault(pair, line.seconds)
                continue

            ends = " -- ".join(str(end or "?") for end in (line.from_place, line.to_place))
            times = ", ".join(line.times) or "no time"
            notes = f" (marked {', '.join(line.notes)})" if line.notes else ""
            questions.append(f"page {line.page + 1}: `{ends}`, {times}{notes}")

    # A place the drawing joins to something, that the survey has never heard
    # of, would make the graph unloadable. Both ends must exist first.
    will_exist = set(known_nodes) | {place.node_id for place in new_places}
    new_edges = {
        pair: seconds
        for pair, seconds in complete.items()
        if all(node in will_exist for node in pair)
    }

    # A link the drawing has between two places the survey already knows is
    # not new data - it is a contradiction. Somebody walked that floor and did
    # not record this link, or recorded a different one. Report it and add
    # nothing: a floor is either surveyed or it is not.
    for pair in sorted(new_edges, key=sorted):
        if all(node in known_nodes for node in pair):
            first, second = sorted(pair)
            disagreements.append(
                f"the drawing joins `{first}` to `{second}` ({new_edges[pair]}s), which "
                f"the survey does not have. Not added - that floor is already surveyed."
            )
    new_edges = {
        pair: seconds
        for pair, seconds in new_edges.items()
        if not all(node in known_nodes for node in pair)
    }

    # Links a person settled from the review file. These skip every check
    # above on purpose: the checks exist to stop the drawing being read too
    # confidently, and somebody who walked the building is not the drawing.
    confirmed: dict[frozenset[str], dict[str, str]] = {}
    for row in read_confirmed_links():
        pair = frozenset((row["from"], row["to"]))
        if pair in known_pairs or not pair <= will_exist:
            continue
        confirmed[pair] = row
        new_edges.setdefault(pair, int(row["walk_seconds"]))

    # -- positions ---------------------------------------------------------

    floor_of: dict[str, tuple[str, str]] = {
        node_id: (node["building"], node["floor"]) for node_id, node in known_nodes.items()
    }
    for place in new_places:
        floor_of[place.node_id] = (place.building, place.floor)

    every_pair = dict(new_edges)
    for pair, edge in known_pairs.items():
        every_pair[pair] = edge["walk_seconds"]

    coordinates, scales, scale_notes = fit_positions(
        positions, floor_of, every_pair, will_exist
    )

    # -- report ------------------------------------------------------------

    lines_out = [
        "# Survey review",
        "",
        "Written by `scripts/import_survey.py`. Everything below is something the",
        "drawing did not settle on its own. Nothing here has been written to the",
        "survey.",
        "",
        f"## Links needing a person ({len(questions)})",
        "",
        "These lines run off the edge of the slide towards a label instead of",
        "ending on a place - which is how the cross-floor connectors are drawn -",
        "or carry a time the surveyor left as `?s`.",
        "",
    ]
    lines_out += [f"- {question}" for question in questions] or ["- none"]
    lines_out += ["", f"## Disagreements with the committed survey ({len(disagreements)})", ""]
    lines_out += [f"- {item}" for item in disagreements] or ["- none"]

    drawn_twice = sorted(
        name
        for name in drawn
        if name not in positions and node_id_for(name) in will_exist
    )
    lines_out += [
        "",
        f"## Places drawn in two spots, so given no position ({len(drawn_twice)})",
        "",
        "A crossing is drawn on both buildings' plans, so its end appears twice.",
        "These places route normally; they just do not draw on a plan yet.",
        "",
    ]
    lines_out += [f"- `{name}`" for name in drawn_twice] or ["- none"]

    islands = unreachable_groups(will_exist, set(every_pair))
    lines_out += [
        "",
        f"## Places nothing connects to the rest ({sum(len(group) for group in islands[1:])})",
        "",
        "Routing between two places in different groups returns no route at all.",
        "Every group after the first is waiting on a connector from the list above -",
        "the links between floors are the ones the drawing does not settle.",
        "",
    ]
    lines_out += [
        f"- {len(group)} places: {', '.join(f'`{node}`' for node in sorted(group))}"
        for group in islands[1:]
    ] or ["- none, every place can be walked to from every other"]

    lines_out += ["", "## Scale fitted for each floor", ""]
    lines_out += [f"- {note}" for note in scale_notes] or ["- none"]

    summary = (
        f"{len(new_places)} places and {len(new_edges)} links to add; "
        f"{len(coordinates)} places positioned"
    )

    # The review and the names file are working notes, not survey data, so
    # they are written either way. That ordering is the point: a dry run hands
    # you the list of places still called "B3 D" so you can name them before
    # anything reaches the survey, where this script will not rename them.
    REVIEW.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    write_places_csv(new_places)
    print("\n".join(lines_out))
    print(f"\nwrote {REVIEW.relative_to(PROJECT_ROOT)} and {PLACES_CSV.relative_to(PROJECT_ROOT)}")

    if not arguments.write:
        unnamed = [place for place in new_places if place.name == readable(place.node_id.replace("_", "-"))]
        print(f"\nwould add {summary}")
        if unnamed:
            print(
                f"{len(unnamed)} of them still have a made-up name. Edit "
                f"{PLACES_CSV.relative_to(PROJECT_ROOT)} first - this script will not "
                f"rename a place once it is in the survey."
            )
        print("re-run with --write to apply")
        return 0

    updated = build_survey(survey, new_places, new_edges, coordinates, confirmed)
    SURVEY.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {SURVEY.relative_to(PROJECT_ROOT)}: {summary}")
    return 0


def _is_walkway(first: str, second: str) -> bool:
    return first.startswith("Hive_SS") or second.startswith("Hive_SS")


def _yes(written: str | None, *, default: bool) -> bool:
    """A yes/no column, with the drawing's own guess when the column is blank."""
    if written is None or written.strip() == "":
        return default
    return written.strip().lower() in {"true", "yes", "y", "1"}


def build_survey(
    survey: dict,
    new_places: list[NewPlace],
    new_edges: dict[frozenset[str], int],
    coordinates: dict[str, tuple[float, float]],
    confirmed: dict[frozenset[str], dict[str, str]] | None = None,
) -> dict:
    """The survey with the new places and links folded in.

    Committed places keep every field they had; a position is only ever added,
    never moved. Re-running with the same drawing therefore produces no diff,
    which is what makes it safe to run whenever the map is redrawn.
    """
    nodes = [dict(node) for node in survey["nodes"]]
    for node in nodes:
        if node["id"] in coordinates and "x" not in node:
            node["x"], node["y"] = coordinates[node["id"]]

    for place in sorted(new_places, key=lambda item: item.node_id):
        node = {
            "id": place.node_id,
            "name": place.name,
            "building": place.building,
            "floor": place.floor,
            "type": place.type,
        }
        if place.node_id in coordinates:
            node["x"], node["y"] = coordinates[place.node_id]
        nodes.append(node)

    edges = [dict(edge) for edge in survey["edges"]]
    confirmed = confirmed or {}
    for pair in sorted(new_edges, key=sorted):
        first, second = sorted(pair)
        seconds = new_edges[pair]
        settled = confirmed.get(pair, {})
        edge = {
            "id": f"{first}--{second}",
            "from": first,
            "to": second,
            "distance_m": metres_for(seconds),
            "walk_seconds": seconds,
            # The linkway between the buildings is the one stretch of this
            # survey that is out in the weather. Every other link measured so
            # far is indoors, which is why asking for a dry route has never
            # changed anything.
            "covered": _yes(settled.get("covered"), default=not _is_walkway(first, second)),
            "stairs": _yes(settled.get("stairs"), default=False),
            "lift": _yes(settled.get("lift"), default=False),
            "blocked": False,
        }
        if settled.get("note"):
            edge["note"] = settled["note"]
        edges.append(edge)

    return {**survey, "nodes": nodes, "edges": edges}


if __name__ == "__main__":
    raise SystemExit(main())

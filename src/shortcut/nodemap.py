"""Read the surveyed node map out of the PDF the team drew it in.

The node map is not a picture of a graph. It is a vector drawing with a text
layer, and every part of the survey is separately addressable:

* a **place** is a small filled square, all the same size on a given page
* its **name** is the ``Hive-B5-G``-style word nearest that square
* a **link** is a straight line drawn between two squares
* its **walking time** is a ``21s`` word sitting on that line
* the **floorplan** is a full-resolution image embedded on the page

So the survey can be read rather than retyped. That matters for more than
convenience: a hand-typed survey is a hundred chances to transpose a digit,
and nobody can check it against anything. This can be re-run, and it is
pinned by a test that makes it reproduce the two floors somebody already
entered by hand, edge for edge.

What it deliberately will not do is guess. Roughly a dozen lines per page run
off the edge of the slide towards a text label rather than ending on a square
- those are the cross-floor connectors, the lift and the staircases - and a
few times were left as ``?s`` by the surveyor. Both are reported for a human
to settle. An importer that quietly invented a staircase would be worse than
no importer at all, because the mistake would look like data.

Positions come out as a **fraction of the floorplan image**, not as pixels.
The same plan appears in this PDF at several resolutions and in the team's
``Maps`` folder at a higher one again, and a fraction is true of all of them.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf

__all__ = [
    "Extraction",
    "Line",
    "Place",
    "Plan",
    "SURVEY_PAGES",
    "WALKING_SPEED_MS",
    "extract_page",
    "node_id_for",
    "read_node_map",
    "read_uploaded_pdf",
]


# The pages worth reading, and what each one covers. The same buildings are
# drawn twice in this PDF: pages 1-5 are one floor per page, pages 7-11 are a
# later redraw that adds the SS and S3 links and carries more times. The later
# ones win, and they are the only pages read, so no cross-page join is needed
# and there is one source of truth per floor.
SURVEY_PAGES: tuple[int, ...] = (6, 7, 8)

#: Metres per second, the figure the survey's walking times were converted at.
#: Kept here so a new edge is priced exactly like the ones already committed.
WALKING_SPEED_MS = 1.4

# A place's name. Deliberately narrow: it must start with a building this
# survey knows, so a stray word in the drawing is not read as a place.
_PLACE_NAME = re.compile(r"^(?:Hive|SS|S3)(?:-[A-Za-z0-9]+)+$")

# A walking time written on a link. "?s" is the surveyor's own mark for one
# they did not measure, and is carried through as a question rather than a zero.
_TIME = re.compile(r"^(\d+|\?)s$")

# The floor a page is titled with. A page headed "Hive + SS B4" carries a
# bare "B4"; a place label carries its floor joined to the rest of the name
# ("Hive-B4-C"), so a standalone token like this only ever comes from the
# title.
_PAGE_FLOOR = re.compile(r"^B[0-9]$")

# How near a line's end must come to a square to count as touching it, as a
# multiple of that page's square size. Ends that touch sit about half a square
# away, because the line is drawn to the square's edge rather than its middle.
_SNAP = 1.2

# How near a time must sit to a line to be read as that line's time, again as
# a multiple of the square size. Every clean link on every page matched at a
# distance of zero, so this is generous by a wide margin.
_TIME_REACH = 1.5


@dataclass(frozen=True)
class Plan:
    """One floorplan image as it is placed on a page."""

    xref: int
    width: int
    height: int
    x0: float
    y0: float
    x1: float
    y1: float

    def contains(self, point: tuple[float, float]) -> bool:
        x, y = point
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def fraction_of(self, point: tuple[float, float]) -> tuple[float, float]:
        """Where a point sits on the plan, as a fraction of its width and height.

        A fraction rather than a pixel because the same plan is embedded here
        at one resolution and kept in the team's Maps folder at another. A
        fraction is true of both; a pixel is true of neither for long.
        """
        x, y = point
        return ((x - self.x0) / (self.x1 - self.x0), (y - self.y0) / (self.y1 - self.y0))


@dataclass(frozen=True)
class Place:
    """A square on the drawing, and the name written beside it."""

    name: str
    page: int
    at: tuple[float, float]
    plan: Plan | None
    #: How far the name sat from the square, in points. A large gap means two
    #: places were drawn close together and the pairing is worth checking.
    name_gap: float

    @property
    def fraction(self) -> tuple[float, float] | None:
        return self.plan.fraction_of(self.at) if self.plan else None


@dataclass(frozen=True)
class Line:
    """A line on the drawing, and what it turned out to join."""

    page: int
    start: tuple[float, float]
    end: tuple[float, float]
    #: The place each end touches, or ``None`` when the end touches nothing -
    #: which is how the cross-floor connectors are drawn.
    from_place: str | None
    to_place: str | None
    #: Every time written near this line. One is the answer; none or several
    #: is a question for the review file.
    times: tuple[str, ...]
    #: Words other than a time sitting on the line - "stairs", "Wheelchair
    #: staircase". They mark how a link is walked and cannot be guessed from
    #: the geometry.
    notes: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        """Whether this line can become an edge without anyone being asked."""
        return (
            self.from_place is not None
            and self.to_place is not None
            and self.from_place != self.to_place
            and len(self.times) == 1
            and self.times[0] != "?s"
        )

    @property
    def seconds(self) -> int | None:
        if len(self.times) != 1 or self.times[0] == "?s":
            return None
        return int(self.times[0][:-1])


@dataclass(frozen=True)
class Extraction:
    """Everything one page yielded."""

    page: int
    plans: tuple[Plan, ...]
    places: tuple[Place, ...]
    lines: tuple[Line, ...]
    #: The floor this page is titled with - "B4" from a page headed "Hive + SS
    #: B4" - or "" on a page that does not say.
    #:
    #: Worth having because a place is not always named after the floor it is
    #: on. The walkway, the canteen and the four unnamed Hive doors are all
    #: labelled without one, and the only thing that says which floor they
    #: belong to is the page they were drawn on. That is not a guess: the page
    #: says so in its own title.
    floor: str = ""


def node_id_for(name: str) -> str:
    """``Hive-B5-G`` -> ``Hive_B5_G``, the form the survey file already uses."""
    return name.replace("-", "_")


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _distance_to_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    """How far a point sits from a line, measured to the nearest part of it.

    Distance to the infinite line would put a time written past the end of a
    short link closer to it than to the link it actually labels.
    """
    (px, py), (ax, ay), (bx, by) = point, start, end
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return _distance(point, start)
    along = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_squared))
    return math.hypot(px - (ax + along * dx), py - (ay + along * dy))


def _square_size(page: pymupdf.Page) -> float:
    """The size of this page's place markers, measured rather than assumed.

    Every page is the same drawing at a different zoom - the squares are 21.9
    points across on one page and 10.8 on another - so a hardcoded size finds
    the places on one page and nothing at all on the next. The markers are the
    most common near-square shape on the page, by a wide margin.
    """
    sizes: Counter[float] = Counter()
    for group in page.get_drawings():
        for item in group["items"]:
            if item[0] != "re":
                continue
            rect = item[1]
            if abs(rect.width - rect.height) < 2 and 5 < rect.width < 40:
                sizes[round(rect.width, 1)] += 1
    if not sizes:
        raise ValueError(f"page {page.number + 1} has no place markers on it")
    return sizes.most_common(1)[0][0]


def _squares_and_lines(
    page: pymupdf.Page, size: float
) -> tuple[list[tuple[float, float]], list[tuple[tuple[float, float], tuple[float, float]]]]:
    """The place markers and the links, deduplicated.

    The pages are exported with their whole content drawn twice, so every
    square and every line is found two or three times over. Deduplicating by
    position is what makes the counts come out right.
    """
    squares: list[tuple[float, float]] = []
    lines: list[tuple[tuple[float, float], tuple[float, float]]] = []

    for group in page.get_drawings():
        for item in group["items"]:
            if item[0] == "re":
                rect = item[1]
                if abs(rect.width - size) > 1.5 or abs(rect.height - size) > 1.5:
                    continue
                middle = ((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
                if not any(_distance(middle, other) < 5 for other in squares):
                    squares.append(middle)
            elif item[0] == "l":
                start, end = (item[1].x, item[1].y), (item[2].x, item[2].y)
                already = any(
                    _distance(start, a) < 3 and _distance(end, b) < 3 for a, b in lines
                )
                if not already:
                    lines.append((start, end))

    return squares, lines


def _plans(page: pymupdf.Page) -> list[Plan]:
    """The floorplan images on this page, largest first, thumbnails dropped.

    A page carries the same image twice: once as the plan being annotated and
    once as a small key in the corner. Only the big one is a plan.
    """
    found: dict[int, Plan] = {}
    for image in page.get_images(full=True):
        xref, _, width, height = image[0], image[1], image[2], image[3]
        for rect in page.get_image_rects(xref):
            if rect.width < 250:
                continue
            existing = found.get(xref)
            if existing is None or rect.width > (existing.x1 - existing.x0):
                found[xref] = Plan(xref, width, height, rect.x0, rect.y0, rect.x1, rect.y1)
    return sorted(found.values(), key=lambda plan: -(plan.x1 - plan.x0))


def _words(page: pymupdf.Page) -> list[tuple[str, tuple[float, float]]]:
    """Every word with its middle, after rejoining names the text layer split.

    ``Hive-SS-Walkway-D`` is stored as ``Hive-SS-`` on one line and
    ``Walkway-D`` on the next. Left alone it matches nothing, and the place it
    names disappears from the survey.
    """
    raw = [(w[4], (w[0], w[1], w[2], w[3])) for w in page.get_text("words")]
    joined: list[tuple[str, tuple[float, float]]] = []
    index = 0
    while index < len(raw):
        text, box = raw[index]
        if text.endswith("-") and index + 1 < len(raw):
            next_text, next_box = raw[index + 1]
            # The continuation sits directly under the first half and starts
            # at the same left edge; nothing else in these drawings does.
            below = 0 < next_box[1] - box[1] < 40 and abs(next_box[0] - box[0]) < 12
            if below and _PLACE_NAME.match(text + next_text):
                middle = ((box[0] + next_box[2]) / 2, (box[1] + next_box[3]) / 2)
                joined.append((text + next_text, middle))
                index += 2
                continue
        joined.append((text, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)))
        index += 1
    return joined


def extract_page(document: pymupdf.Document, page_number: int) -> Extraction:
    """Read one page of the node map into places and links."""
    page = document[page_number]
    size = _square_size(page)
    squares, raw_lines = _squares_and_lines(page, size)
    plans = _plans(page)
    words = _words(page)

    names = [(text, at) for text, at in words if _PLACE_NAME.match(text)]
    times = [(text, at) for text, at in words if _TIME.match(text)]
    # Words that describe how a link is walked. Kept as written; deciding what
    # they mean for an edge is the reviewer's job, not this file's.
    marks = [
        (text, at)
        for text, at in words
        if text.lower() in {"stairs", "staircase", "wheelchair", "lift", "escalator"}
    ]

    # Each square takes the nearest unclaimed name. Nearest-first across all
    # pairs rather than square-by-square, so two places drawn close together
    # cannot both grab the same label and leave the other unnamed.
    pairs = sorted(
        (
            (_distance(square, at), square_index, name_index)
            for square_index, square in enumerate(squares)
            for name_index, (_, at) in enumerate(names)
        )
    )
    taken_squares: set[int] = set()
    taken_names: set[int] = set()
    places: list[Place] = []
    for gap, square_index, name_index in pairs:
        if square_index in taken_squares or name_index in taken_names:
            continue
        taken_squares.add(square_index)
        taken_names.add(name_index)
        at = squares[square_index]
        plan = next((candidate for candidate in plans if candidate.contains(at)), None)
        places.append(Place(names[name_index][0], page_number, at, plan, gap))

    by_position = {place.at: place.name for place in places}

    # A time belongs to exactly one link. Letting every link within reach claim
    # it instead reads a "3s" written in a corner as the time of both links
    # that meet there, and turns two good rows into two questions.
    claimed: dict[int, list[str]] = {}
    for text, at in times:
        distances = [
            (_distance_to_segment(at, start, end), index)
            for index, (start, end) in enumerate(raw_lines)
        ]
        if not distances:
            continue
        nearest, index = min(distances)
        if nearest <= size * _TIME_REACH:
            claimed.setdefault(index, []).append(text)

    lines: list[Line] = []
    for index, (start, end) in enumerate(raw_lines):
        ends: list[str | None] = []
        for point in (start, end):
            if not squares:
                ends.append(None)
                continue
            nearest_square = min(squares, key=lambda square: _distance(point, square))
            touching = _distance(point, nearest_square) <= size * _SNAP
            ends.append(by_position.get(nearest_square) if touching else None)

        on_line = claimed.get(index, [])
        # A time the surveyor later replaced is still in the file, drawn under
        # its replacement. When both land on the same link, the measured one is
        # the answer and the question mark is spent.
        if len(on_line) > 1 and any(text != "?s" for text in on_line):
            on_line = [text for text in on_line if text != "?s"]

        lines.append(
            Line(
                page=page_number,
                start=start,
                end=end,
                from_place=ends[0],
                to_place=ends[1],
                times=tuple(on_line),
                notes=tuple(
                    text
                    for text, at in marks
                    if _distance_to_segment(at, start, end) <= size * _TIME_REACH * 2
                ),
            )
        )

    titled = [text for text, _ in words if _PAGE_FLOOR.match(text)]

    return Extraction(
        page_number,
        tuple(plans),
        tuple(places),
        tuple(lines),
        # Only when the page says one thing. Two would mean this is not a
        # title at all, and inventing a floor is the mistake being avoided.
        floor=titled[0] if len(set(titled)) == 1 else "",
    )


def read_node_map(
    path: Path, pages: tuple[int, ...] = SURVEY_PAGES
) -> tuple[Extraction, ...]:
    """Read every surveyed page of the node map."""
    with pymupdf.open(path) as document:
        return tuple(extract_page(document, page) for page in pages)


def read_uploaded_pdf(content: bytes) -> tuple[Extraction, ...]:
    """Read a drawing somebody has just uploaded, without knowing its shape.

    Two things differ from :func:`read_node_map`, and both come from not
    having seen the file before.

    **Every page is read**, because :data:`SURVEY_PAGES` describes one
    particular file - the pages of *this* team's node map that supersede the
    earlier redraw - and says nothing about a drawing nobody has looked at
    yet. Guessing a page range for an unknown file would silently drop
    whichever floors happened to fall outside it.

    **A page with nothing on it is skipped rather than fatal.** A title page
    or a legend has no place markers, and refusing the whole upload because
    page one is a cover sheet would reject most real documents.

    Reading everything means an older page can offer a place that contradicts
    a newer one. That is not resolved here, and deliberately so: this returns
    candidates for a person to accept or reject, and the reviewer is the one
    who knows which page is current.
    """
    extractions: list[Extraction] = []
    with pymupdf.open(stream=content, filetype="pdf") as document:
        for number in range(document.page_count):
            try:
                extractions.append(extract_page(document, number))
            except ValueError:
                continue  # no place markers on this page: not a survey page
    return tuple(extractions)


def metres_for(seconds: int) -> float:
    """Walking seconds priced into metres, the way the rest of the survey is."""
    return round(seconds * WALKING_SPEED_MS, 1)


def fit_metres_per_pixel(
    edges: list[tuple[float, float]], plan_width: int
) -> tuple[float, float]:
    """One scale for a floor, fitted across every link drawn on it.

    Each link gives the same equation twice over: it is so many pixels long on
    the plan, and so many metres long in the building. One link would settle
    the scale on its own and be wrong by however much that one link was
    mismeasured, so all of them are used and the spread is reported. A floor
    whose links disagree is a floor whose tracing is wrong, and the number that
    says so is worth more than the scale itself.

    Returns the scale, and the worst disagreement as a fraction.
    """
    ratios = [metres / pixels for pixels, metres in edges if pixels > 0]
    if not ratios:
        raise ValueError("no links to fit a scale from")
    scale = statistics.median(ratios)
    worst = max(abs(ratio - scale) / scale for ratio in ratios)
    return scale, worst

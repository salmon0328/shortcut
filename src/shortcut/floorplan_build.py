"""Building a floorplan for one floor out of the drawing it was traced on.

A floor is not always one picture. The node map draws Hive B3, SS B3 and S3 B3
side by side on a single page as three separate images, and the places on each
belong to the same floor of the same campus. Uploading only the biggest of them
- which is what the command line tool does - leaves every place on the other
two with nowhere to be drawn, and no scale to be measured against.

So this composes them: one image per building and floor, made by placing each
crop where the PDF itself says it goes. That placement is not estimated. Every
image on a page carries its own rectangle in page points, and every place was
read in those same points, so the crops and the places are already in one
coordinate system before anything here runs. The composite is rasterised from
a page holding only the images, so the surveyor's own squares and lines stay
out of it - the app draws its own, and a floorplan with them baked in would
show everything twice.

**The scale is fitted, and how it is fitted matters.** Each link on the floor
is evidence: the drawing says it is so many points long, the survey says it is
so many metres. The *smallest* of those ratios is taken, never the average.
A* only returns the shortest route while its straight-line estimate never
exceeds the real walk, and a median lets half the links come out longer on the
plan than in the building - which was tried, and quietly made 76 of 1560 trips
worse. The smallest ratio is the largest scale at which every link stays
within its surveyed length, so the estimate is always a little short and never
once too long.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pymupdf

from shortcut.nodemap import Extraction, Plan, metres_for, node_id_for

__all__ = [
    "MIN_LINKS_FOR_A_SCALE",
    "FloorplanDraft",
    "build_drafts",
]

#: How many links a floor needs before its fitted scale is believed. One or
#: two links can agree perfectly and still both be wrong, and a place
#: confidently drawn in the wrong room is worse than one the map admits it
#: cannot place: the first is never questioned, the second says so on screen.
MIN_LINKS_FOR_A_SCALE = 3

#: Rendering resolution, as a multiple of the crops' own pixels per point. 1.0
#: keeps roughly the detail the drawing already has; going higher only invents
#: pixels, and going lower throws away detail somebody scanned.
_RESOLUTION = 1.0

#: How far the links may disagree before the fit stops being worth trusting.
#:
#: Some disagreement is expected and means nothing: a corridor that bends is
#: longer walked than drawn, so its ratio comes out high. On this drawing the
#: floors that check out against a hand-calibrated plan spread by 1.5x to
#: 2.3x. The walkway between the buildings spreads by 9x and produces a floor
#: 19m wide that has a 24m leg in it - the links there are not describing one
#: flat picture, and no single scale can make them.
#:
#: Above this the plan is still offered, with the number attached: refusing
#: outright would leave somebody with no way to place a floor the drawing
#: genuinely covers, and the reviewer can see the image.
WELL_CONDITIONED_SPREAD = 3.0


@dataclass(frozen=True)
class FloorplanDraft:
    """One floor's plan, composed and measured, ready for somebody to approve."""

    building: str
    floor: str
    image: bytes
    content_type: str
    width_px: int
    height_px: int
    metres_per_pixel: float
    #: Always zero: the composite's own top-left corner is the origin, so the
    #: image and the positions below agree by construction rather than by
    #: anybody remembering to keep them in step.
    origin_x_m: float = 0.0
    origin_y_m: float = 0.0
    #: How many links voted on the scale, and how much the tightest disagreed
    #: with the loosest. A floor whose links disagree is a floor traced badly,
    #: and the number saying so is worth more than the scale itself.
    links_used: int = 0
    spread: float = 1.0
    #: Where each place on this floor lands, in metres on this image.
    places: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: Which crops it was built from, and where they came from.
    source: str = ""
    made_of: int = 1
    #: True when the store already holds a plan for this floor, so approving
    #: this one replaces it rather than adding a second.
    replaces_existing: bool = False

    @property
    def label(self) -> str:
        where = " · ".join(part for part in (self.building, self.floor) if part)
        return f"Floorplan for {where}"

    @property
    def well_conditioned(self) -> bool:
        """Whether the links agree closely enough for the scale to be believed."""
        return self.spread <= WELL_CONDITIONED_SPREAD

    @property
    def measures(self) -> tuple[float, float]:
        """How big this floor comes out, in metres. The sanity check a person
        can actually do: a floor that reads 19m wide with a 24m walk in it is
        wrong however good the arithmetic looked."""
        return (
            round(self.width_px * self.metres_per_pixel, 1),
            round(self.height_px * self.metres_per_pixel, 1),
        )


def _union(plans: list[Plan]) -> tuple[float, float, float, float]:
    return (
        min(plan.x0 for plan in plans),
        min(plan.y0 for plan in plans),
        max(plan.x1 for plan in plans),
        max(plan.y1 for plan in plans),
    )


def _pixels_per_point(plans: list[Plan]) -> float:
    """The finest detail any crop has, so none of them is downsampled."""
    best = 0.0
    for plan in plans:
        width_pts = plan.x1 - plan.x0
        if width_pts > 0:
            best = max(best, plan.width / width_pts)
    return best or 1.0


def _render_dpi(plans: list[Plan]) -> int:
    return round(72 * _pixels_per_point(plans) * _RESOLUTION)


def _size_in_pixels(
    plans: list[Plan], box: tuple[float, float, float, float]
) -> tuple[int, int]:
    """How big the composite comes out, without drawing it.

    The same arithmetic the renderer does, so measuring a floor and composing
    it later cannot disagree about its size - and the size is what the scale
    is expressed against.
    """
    # Ceil, not round: the renderer never drops the last part-covered pixel,
    # and a size measured here that disagreed with the one drawn later would
    # put the reviewed scale a hair off the stored one.
    dpi = _render_dpi(plans)
    return (
        math.ceil((box[2] - box[0]) * dpi / 72),
        math.ceil((box[3] - box[1]) * dpi / 72),
    )


def _compose(
    document: pymupdf.Document, plans: list[Plan], box: tuple[float, float, float, float]
) -> tuple[bytes, int, int]:
    """Rasterise the crops into one image, each where the PDF puts it.

    Drawn onto a page of its own rather than composited by hand: the placement
    arithmetic is the renderer's, which is the same one that decided where the
    crops sit in the first place.
    """
    x0, y0, x1, y1 = box
    scratch = pymupdf.open()
    page = scratch.new_page(width=x1 - x0, height=y1 - y0)

    for plan in plans:
        extracted = document.extract_image(plan.xref)
        page.insert_image(
            pymupdf.Rect(plan.x0 - x0, plan.y0 - y0, plan.x1 - x0, plan.y1 - y0),
            stream=extracted["image"],
        )

    pixmap = page.get_pixmap(dpi=_render_dpi(plans))
    return pixmap.tobytes("png"), pixmap.width, pixmap.height


def _fit_metres_per_point(
    at: dict[str, tuple[float, float]], links: dict[frozenset[str], float]
) -> tuple[float | None, int, float]:
    """Metres per page point for one floor, from the links drawn across it.

    Returns ``(scale, how many links voted, spread)``. A scale of ``None``
    means too few links joined two placed places to trust one.
    """
    ratios: list[float] = []
    for pair, seconds in links.items():
        first, second = sorted(pair)
        if first not in at or second not in at:
            continue
        (ax, ay), (bx, by) = at[first], at[second]
        length = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
        if length > 0:
            ratios.append(metres_for(seconds) / length)

    if len(ratios) < MIN_LINKS_FOR_A_SCALE:
        return None, len(ratios), 1.0

    # See the module docstring: the smallest, never the median.
    scale = min(ratios)
    return scale, len(ratios), max(ratios) / scale


def build_drafts(
    pdf: bytes,
    extractions: tuple[Extraction, ...],
    floor_of: dict[str, tuple[str, str]],
    *,
    filename: str = "",
    already_have: set[tuple[str, str]] | None = None,
    compose: bool = True,
) -> tuple[list[FloorplanDraft], list[str]]:
    """A floorplan for every building and floor the drawing covers.

    ``floor_of`` says which building and floor each place belongs to, which is
    a decision made elsewhere - a place is not always named after the floor it
    is on, and the page title settles the ones that are not.

    ``compose`` draws the images. Measuring a floor is geometry and takes no
    time; rasterising five plans takes about a second, and a review screen
    only needs the numbers. So the upload measures, and the image is drawn
    when somebody approves the plan and it is actually going to be kept.

    Returns the drafts and a note for every floor that could not get one, in
    the words somebody reviewing the upload would want to read.
    """
    already_have = already_have or set()
    drafts: list[FloorplanDraft] = []
    notes: list[str] = []
    where = f"{filename} " if filename else ""

    # Best evidence per floor, so a floor drawn on more than one page ends up
    # with one plan rather than one per page.
    best: dict[tuple[str, str], FloorplanDraft] = {}
    refused: dict[tuple[str, str], str] = {}

    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        for extraction in extractions:
            # Titled pages only. This drawing holds the Hive twice - an older
            # redraw on pages 1-5 and the current one on 7-11 - and the older
            # pages disagree about lettering. The title is what says which
            # floor a page is, so a page without one has nothing to say about
            # which floor its crops belong to either.
            if not extraction.floor:
                continue

            # Places grouped by the floor they are on, carrying the page
            # points they were drawn at - one coordinate system for the whole
            # page, whichever crop each one happens to sit on.
            by_floor: dict[tuple[str, str], dict[str, tuple[float, float]]] = {}
            plans_by_floor: dict[tuple[str, str], dict[int, Plan]] = {}

            for place in extraction.places:
                node_id = node_id_for(place.name)
                key = floor_of.get(node_id)
                if key is None or place.plan is None:
                    continue
                by_floor.setdefault(key, {})[node_id] = place.at
                plans_by_floor.setdefault(key, {})[place.plan.xref] = place.plan

            # What the drawing says each link takes, for the scale fit.
            links: dict[frozenset[str], float] = {}
            for line in extraction.lines:
                if line.is_complete:
                    pair = frozenset(
                        (node_id_for(line.from_place), node_id_for(line.to_place))
                    )
                    links.setdefault(pair, line.seconds)

            for key, at in sorted(by_floor.items()):
                building, floor = key
                plans = list(plans_by_floor[key].values())
                scale_per_point, voted, spread = _fit_metres_per_point(at, links)

                if scale_per_point is None:
                    refused.setdefault(
                        key,
                        f"{building} {floor or '(no floor)'}: only {voted} link(s) join "
                        "two places drawn on the plan, too few to trust a scale, so no "
                        "floorplan was built.",
                    )
                    continue

                # A floor drawn on two pages keeps whichever reading rests on
                # more links. More votes is the only thing that makes one fit
                # better than another from here.
                existing = best.get(key)
                if existing is not None and existing.links_used >= voted:
                    continue

                box = _union(plans)
                if compose:
                    image, width_px, height_px = _compose(document, plans, box)
                else:
                    image = b""
                    width_px, height_px = _size_in_pixels(plans, box)

                # From the resolution itself rather than from the rounded
                # pixel count: one pixel is exactly 72/dpi points, while the
                # width is whole pixels and carries the rounding.
                points_per_pixel = 72 / _render_dpi(plans)
                places = {
                    node_id: (
                        round((x - box[0]) * scale_per_point, 2),
                        round((y - box[1]) * scale_per_point, 2),
                    )
                    for node_id, (x, y) in at.items()
                }

                best[key] = FloorplanDraft(
                        building=building,
                        floor=floor,
                        image=image,
                        content_type="image/png",
                        width_px=width_px,
                        height_px=height_px,
                        metres_per_pixel=scale_per_point * points_per_pixel,
                        links_used=voted,
                        spread=spread,
                        places=places,
                        source=f"{where}page {extraction.page + 1}",
                        made_of=len(plans),
                        replaces_existing=key in already_have,
                    )

    drafts = [best[key] for key in sorted(best)]
    # Only complain about a floor nothing could be built for. A floor that
    # failed on one page and worked on another is not a problem anybody needs
    # telling about.
    notes = [note for key, note in sorted(refused.items()) if key not in best]
    return drafts, notes

"""Building a floor's plan out of the drawing it was traced on.

Run against the team's real node map, because what is worth pinning is not
that the code executes - it is that the scale it fits agrees with the one
somebody calibrated by hand, on a drawing that puts three buildings side by
side on one page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shortcut.floorplan_build import (
    MIN_LINKS_FOR_A_SCALE,
    WELL_CONDITIONED_SPREAD,
    FloorplanDraft,
    build_drafts,
)
from shortcut.nodemap import node_id_for, read_uploaded_pdf
from shortcut.survey_import import _building_and_floor

NODE_MAP = (
    Path(__file__).resolve().parents[1] / "data" / "survey_sources" / "hive_node_map.pdf"
)

#: What the plans already in the store measure, from the calibration somebody
#: fitted and uploaded months before any of this existed. The width in metres
#: is the comparable number: the images are a different size, so only what
#: they say about the building can be checked against a fresh reading.
HAND_CALIBRATED_WIDTH_M = {("Hive", "B3"): 68.1, ("Hive", "B4"): 61.0, ("Hive", "B5"): 53.7}


@pytest.fixture(scope="module")
def pdf() -> bytes:
    return NODE_MAP.read_bytes()


@pytest.fixture(scope="module")
def drafts(pdf: bytes) -> list[FloorplanDraft]:
    pages = read_uploaded_pdf(pdf)
    floor_of = {}
    for extraction in pages:
        if not extraction.floor:
            continue
        for place in extraction.places:
            floor_of[node_id_for(place.name)] = _building_and_floor(
                place.name, extraction.floor
            )
    built, _ = build_drafts(pdf, pages, floor_of, filename="hive_node_map.pdf")
    return built


def by_floor(drafts: list[FloorplanDraft], building: str, floor: str) -> FloorplanDraft:
    return next(d for d in drafts if d.building == building and d.floor == floor)


# --------------------------------------------------------------------------
# Does it agree with the scale somebody fitted by hand?
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("building", "floor"), sorted(HAND_CALIBRATED_WIDTH_M))
def test_the_fitted_scale_matches_the_one_calibrated_by_hand(
    building: str, floor: str, drafts: list[FloorplanDraft]
) -> None:
    """The reason to trust this on floors nobody has calibrated.

    The image is a different size from the one in the store, so the pixels
    cannot be compared - what can is how wide the floor comes out in metres,
    which is a fact about the building rather than about either picture.
    """
    draft = by_floor(drafts, building, floor)
    width_m, _ = draft.measures
    expected = HAND_CALIBRATED_WIDTH_M[(building, floor)]

    assert width_m == pytest.approx(expected, rel=0.05)


def test_a_floor_drawn_across_three_images_becomes_one_plan(
    drafts: list[FloorplanDraft],
) -> None:
    """SS B3 is drawn as three crops on one page. Uploading the biggest, which
    is what the command line tool does, leaves the places on the other two
    with no image to be drawn on."""
    draft = by_floor(drafts, "SS", "B3")

    assert draft.made_of == 3
    assert draft.places, "the places on every crop belong to the composed plan"


def test_every_place_on_the_floor_lands_inside_its_own_plan(
    drafts: list[FloorplanDraft],
) -> None:
    """The image and the positions have to agree, and they agree by
    construction here - both are measured from the composite's top-left."""
    for draft in drafts:
        width_m, height_m = draft.measures
        for node_id, (x, y) in draft.places.items():
            assert 0 <= x <= width_m, f"{node_id} is off {draft.label} horizontally"
            assert 0 <= y <= height_m, f"{node_id} is off {draft.label} vertically"


def test_the_origin_is_the_composite_itself(drafts: list[FloorplanDraft]) -> None:
    """Anything else would need the image and the positions kept in step by
    somebody remembering to, which is the kind of thing that stops happening."""
    assert all(d.origin_x_m == 0.0 and d.origin_y_m == 0.0 for d in drafts)


# --------------------------------------------------------------------------
# What it refuses, and what it warns about
# --------------------------------------------------------------------------


def test_a_floor_with_too_few_links_gets_no_plan(pdf: bytes) -> None:
    """One or two links can agree perfectly and still both be wrong, and a
    place confidently drawn in the wrong room is worse than one the map admits
    it cannot place."""
    pages = read_uploaded_pdf(pdf)
    floor_of = {}
    for extraction in pages:
        if not extraction.floor:
            continue
        for place in extraction.places:
            floor_of[node_id_for(place.name)] = _building_and_floor(
                place.name, extraction.floor
            )

    built, notes = build_drafts(pdf, pages, floor_of)

    refused = {(d.building, d.floor) for d in built}
    assert ("S3", "B3") not in refused, "S3 B3 has one link, too few to scale from"
    assert any("S3 B3" in note for note in notes), "and it says so rather than going quiet"
    assert all(d.links_used >= MIN_LINKS_FOR_A_SCALE for d in built)


def test_a_badly_conditioned_fit_is_flagged_rather_than_hidden(
    drafts: list[FloorplanDraft],
) -> None:
    """The walkway's links disagree by 9x and produce a floor 19m wide with a
    24m leg in it. Offered, because the reviewer can see the picture - but
    never quietly."""
    walkway = by_floor(drafts, "Hive-SS", "B4")

    assert walkway.well_conditioned is False
    assert walkway.spread > WELL_CONDITIONED_SPREAD


def test_the_floors_that_check_out_are_not_flagged(drafts: list[FloorplanDraft]) -> None:
    """A warning everything trips is a warning nobody reads."""
    for building, floor in HAND_CALIBRATED_WIDTH_M:
        assert by_floor(drafts, building, floor).well_conditioned is True


def test_a_floor_is_built_once_however_many_pages_draw_it(
    drafts: list[FloorplanDraft],
) -> None:
    """This drawing holds the Hive twice - an older redraw and the current
    one - and two plans for one floor is one plan too many."""
    keys = [(d.building, d.floor) for d in drafts]

    assert len(keys) == len(set(keys))


def test_it_says_when_a_plan_would_replace_one_already_held(pdf: bytes) -> None:
    pages = read_uploaded_pdf(pdf)
    floor_of = {}
    for extraction in pages:
        if not extraction.floor:
            continue
        for place in extraction.places:
            floor_of[node_id_for(place.name)] = _building_and_floor(
                place.name, extraction.floor
            )

    built, _ = build_drafts(pdf, pages, floor_of, already_have={("Hive", "B5")})

    assert by_floor(built, "Hive", "B5").replaces_existing is True
    assert by_floor(built, "Hive", "B3").replaces_existing is False


# --------------------------------------------------------------------------
# The image itself
# --------------------------------------------------------------------------


def test_the_composed_image_is_a_real_png(drafts: list[FloorplanDraft]) -> None:
    for draft in drafts:
        assert draft.image.startswith(b"\x89PNG\r\n\x1a\n")
        assert draft.content_type == "image/png"
        assert draft.width_px > 0 and draft.height_px > 0


def test_reading_the_same_drawing_twice_builds_the_same_plans(pdf: bytes) -> None:
    pages = read_uploaded_pdf(pdf)
    floor_of = {}
    for extraction in pages:
        if not extraction.floor:
            continue
        for place in extraction.places:
            floor_of[node_id_for(place.name)] = _building_and_floor(
                place.name, extraction.floor
            )

    first, _ = build_drafts(pdf, pages, floor_of)
    second, _ = build_drafts(pdf, pages, floor_of)

    assert [(d.building, d.floor, d.metres_per_pixel) for d in first] == [
        (d.building, d.floor, d.metres_per_pixel) for d in second
    ]

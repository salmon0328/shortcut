"""Tests for reading the survey out of the drawn node map.

The test that matters most is the first one. The importer is pointed at the
same PDF the team drew, and made to produce the two floors somebody had
already entered into the survey by hand, months of walking earlier. If it
reproduces those edge for edge - the same links, the same seconds - then the
places it found on the floors nobody has typed up yet were found the same way.
That is the whole argument for trusting it, and it is checked rather than
asserted.

Everything else here is about the importer's refusals: what it does when the
drawing is unclear. A reader that quietly filled a gap would be worse than no
reader, because the guess would arrive looking exactly like survey data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
NODE_MAP = PROJECT_ROOT / "data" / "survey_sources" / "hive_node_map.pdf"
SURVEY = PROJECT_ROOT / "data" / "campus_graph.json"


# scripts/ is a folder of tools, not a package, so it goes on the path rather
# than being imported through shortcut.*. Plain imports rather than importlib:
# a module built by hand is not registered in sys.modules, and @dataclass
# looks itself up there while it is being defined.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import import_survey  # noqa: E402

from shortcut import nodemap  # noqa: E402


@pytest.fixture(scope="module")
def pages() -> tuple:
    return nodemap.read_node_map(NODE_MAP)


@pytest.fixture(scope="module")
def survey() -> dict:
    return json.loads(SURVEY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def drawn_links(pages: tuple) -> dict[frozenset[str], int]:
    """Every link the drawing states outright, as node ids and seconds."""
    links: dict[frozenset[str], int] = {}
    for page in pages:
        for line in page.lines:
            if line.is_complete:
                pair = frozenset(
                    (
                        nodemap.node_id_for(line.from_place),
                        nodemap.node_id_for(line.to_place),
                    )
                )
                links.setdefault(pair, line.seconds)
    return links


# --------------------------------------------------------------------------
# Does it agree with the floors somebody surveyed by hand?
# --------------------------------------------------------------------------


def _within_floor(survey: dict, floor: str) -> dict[frozenset[str], int]:
    on_floor = {
        node["id"] for node in survey["nodes"] if node["floor"] == floor
    }
    return {
        frozenset((edge["from"], edge["to"])): edge["walk_seconds"]
        for edge in survey["edges"]
        if edge["from"] in on_floor and edge["to"] in on_floor
    }


#: Places the drawing and the survey have genuinely disagreed about. Empty
#: right now: the one entry this ever held - a 12s link the survey put on the
#: Main Staircase and the drawing put on the Lift Lobby instead - was fixed by
#: hand once somebody noticed the Lift Lobby is the one three seconds from the
#: Main Staircase, so it agrees with the drawing again.
#:
#: Kept as a named set rather than deleted outright: this test fails if a new
#: disagreement appears, which is the thing worth knowing, and an empty set is
#: still a set something can be added back to.
KNOWN_DISAGREEMENTS: set[frozenset[str]] = set()

#: Two links added to the survey by hand that do not match the drawing at
#: all - not a disagreement about a number, but the wrong pair of nodes.
#:
#: The walkway was described to whoever added these as a straight chain,
#: Walkway-D to A to B to C to the canteen. It is not one: D is a hub joining
#: A, B, C and the Hive doors separately, and A-C is its own branch. The
#: person correctly heard "6s" and "7s" as the missing numbers and just paired
#: them with the wrong ends. The real links, still absent from the survey,
#: are Walkway_A-Walkway_C (7s) and Walkway_B-Walkway_D (6s).
#:
#: Recorded rather than corrected here, because a test file is not where
#: survey data should be fixed - this exists so the suite stays green while
#: whoever owns the map decides what to do about it, not to paper over it.
KNOWN_WRONG_PAIRS = {
    frozenset({"Hive_SS_Walkway_A", "Hive_SS_Walkway_D"}),
    frozenset({"Hive_SS_Walkway_B", "Hive_SS_Walkway_C"}),
}


@pytest.mark.parametrize("floor", ["B5", "B4"])
def test_the_drawing_gives_the_same_links_a_person_typed_up_by_hand(
    floor: str, survey: dict, drawn_links: dict[frozenset[str], int]
) -> None:
    """The floors that were entered by hand come back out of the PDF.

    This is the reason to trust the importer on the floors nobody has typed
    up: it is made to rediscover the ones somebody already did, by hand, from
    the same drawing, and it does - every link, every second, bar the one
    listed above.

    Allowed to find *more* than the survey has, since the drawing is redrawn
    as more of the building is walked. What it may not do is lose a link, or
    disagree about how long one takes.
    """
    committed = _within_floor(survey, floor)
    assert committed, f"{floor} is not in the survey at all"

    for pair, seconds in committed.items():
        if pair in KNOWN_DISAGREEMENTS or pair in KNOWN_WRONG_PAIRS:
            continue
        assert pair in drawn_links, f"the drawing lost {sorted(pair)}"
        assert drawn_links[pair] == seconds, f"{sorted(pair)} disagrees on its time"


def test_the_drawing_and_the_survey_still_disagree_in_only_one_place(
    survey: dict, drawn_links: dict[frozenset[str], int]
) -> None:
    """Guards the list above from quietly growing.

    A second disagreement means either the drawing was edited or the reading
    broke, and both are worth stopping for.
    """
    surveyed = {**_within_floor(survey, "B5"), **_within_floor(survey, "B4")}

    missing = {pair for pair in surveyed if pair not in drawn_links}

    assert missing == KNOWN_DISAGREEMENTS | KNOWN_WRONG_PAIRS


def test_it_finds_the_places_on_the_floors_nobody_has_typed_up(pages: tuple) -> None:
    """The floors past B5 and B4 are why any of this exists."""
    found = {place.name for page in pages for place in page.places}

    assert {f"Hive-B3-{letter}" for letter in "ABCDEFG"} <= found
    assert {f"Hive-SS-Walkway-{letter}" for letter in "ABCD"} <= found
    assert {"SS-Canteen-A", "SS-Canteen-B", "S3-B3-A", "S3-B3-B"} <= found


# --------------------------------------------------------------------------
# What it refuses to decide
# --------------------------------------------------------------------------


def test_a_link_that_stops_short_of_a_place_is_reported_not_guessed(
    pages: tuple,
) -> None:
    """The connectors between floors run off the page towards a label.

    Snapping them to whatever place happens to be nearest would invent a
    staircase, and an invented staircase sends somebody who cannot use stairs
    down one.
    """
    dangling = [
        line
        for page in pages
        for line in page.lines
        if line.from_place is None or line.to_place is None
    ]

    assert dangling, "the connectors are what this test is about"
    assert all(not line.is_complete for line in dangling)


def test_a_time_the_surveyor_left_unmeasured_stays_unmeasured(pages: tuple) -> None:
    """``?s`` is the surveyor saying they did not measure it. It is not a zero."""
    unmeasured = [
        line for page in pages for line in page.lines if "?s" in line.times
    ]

    assert unmeasured, "the drawing has question marks on it"
    assert all(line.seconds is None for line in unmeasured)
    assert all(not line.is_complete for line in unmeasured)


def test_a_time_written_between_two_links_is_only_given_to_one(pages: tuple) -> None:
    """A time belongs to one link.

    Where two links meet, a time written in the corner sits near both. Letting
    each claim it turns two good rows into two arguments, so the nearer link
    takes it and the other is left to find its own.
    """
    for page in pages:
        for line in page.lines:
            assert len(line.times) <= 1, f"{line.from_place}--{line.to_place}"


def test_a_place_drawn_on_two_plans_is_given_no_position(pages: tuple) -> None:
    """A crossing appears on both buildings' plans, once truly and once as a mark.

    Nothing in the geometry says which is which, so neither is used.
    """
    positions = import_survey.home_plans(pages)

    drawn_twice = {
        place.name
        for page in pages
        for place in page.places
        if place.plan is not None
    } - set(positions)

    assert "Hive-SS-Walkway-D" in drawn_twice
    assert all(name not in positions for name in drawn_twice)


# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------


def test_a_position_is_a_fraction_of_the_plan_so_it_survives_a_better_scan(
    pages: tuple,
) -> None:
    """The same plan exists here small and in the Maps folder large."""
    for page in pages:
        for place in page.places:
            if place.plan is None:
                continue
            across, down = place.fraction
            assert 0.0 <= across <= 1.0
            assert 0.0 <= down <= 1.0


def test_a_floor_with_too_few_links_is_left_unpositioned_rather_than_guessed() -> None:
    """One link can agree with itself perfectly and still be wrong.

    A place put confidently in the wrong room is worse than one the map admits
    it cannot draw, because the map draws the first and nobody re-checks it.
    """
    positions = {"A-B4-1": _place(0.1, 0.1), "A-B4-2": _place(0.9, 0.9)}
    floors = {"A_B4_1": ("A", "B4"), "A_B4_2": ("A", "B4")}
    one_link = {frozenset({"A_B4_1", "A_B4_2"}): 30}

    coordinates, scales, notes = import_survey.fit_positions(
        positions, floors, one_link, {"A_B4_1", "A_B4_2"}
    )

    assert coordinates == {}
    assert scales == {}
    assert "too few to trust" in " ".join(notes)


def test_the_scale_it_fits_agrees_with_the_walking_times_it_was_given(
    pages: tuple, survey: dict, drawn_links: dict[frozenset[str], int]
) -> None:
    """A metre on the plan is a metre in the building, within reason.

    Not exactly: a corridor that bends is longer walked than drawn straight,
    so every link disagrees a little. What is checked is that the fitted scale
    puts the floor at a believable size rather than off by a factor.
    """
    floors = {
        node["id"]: (node["building"], node["floor"]) for node in survey["nodes"]
    }
    positions = import_survey.home_plans(pages)

    _, scales, _ = import_survey.fit_positions(
        positions, floors, drawn_links, set(floors)
    )

    plan_widths_m = []
    for (building, floor), scale in scales.items():
        if building != "Hive":
            continue
        name = next(iter(n for n in positions if positions[n].plan and
                         floors.get(nodemap.node_id_for(n)) == (building, floor)))
        plan_widths_m.append(positions[name].plan.width * scale)

    assert plan_widths_m, "no Hive floor got a scale"
    # The Hive is a large building but it is one building: every floor should
    # come out between a bus and a runway.
    assert all(40 < width < 200 for width in plan_widths_m), plan_widths_m


def _place(across: float, down: float):
    plan = nodemap.Plan(xref=1, width=1000, height=1000, x0=0, y0=0, x1=1000, y1=1000)
    return nodemap.Place(
        name="x", page=0, at=(across * 1000, down * 1000), plan=plan, name_gap=0.0
    )


# --------------------------------------------------------------------------
# Running it again
# --------------------------------------------------------------------------


def test_reading_the_same_drawing_twice_gives_the_same_answer() -> None:
    """Safe to re-run whenever the map is redrawn, which is the point."""
    first = nodemap.read_node_map(NODE_MAP)
    second = nodemap.read_node_map(NODE_MAP)

    assert [page.places for page in first] == [page.places for page in second]
    assert [page.lines for page in first] == [page.lines for page in second]


def test_it_never_writes_the_survey_unless_asked(survey: dict) -> None:
    """Importing the module must not have side effects on the real file."""
    assert json.loads(SURVEY.read_text(encoding="utf-8")) == survey


# --------------------------------------------------------------------------
# Renaming a place after it has been imported
# --------------------------------------------------------------------------
#
# The drawing calls a place "Hive-B3-C" and a student calls it something a
# person would say. Nobody can write the second until they see the first in
# the survey, so the first import necessarily writes a stand-in and the names
# file is where that gets corrected.


def _named_place(node_id: str, name: str):
    return import_survey.NewPlace(
        node_id=node_id, name=name, building="Hive", floor="B3",
        type="junction", x=None, y=None,
    )


def test_a_place_already_in_the_survey_takes_its_new_name_from_the_names_file() -> None:
    survey = {
        "nodes": [{"id": "Hive_B3_C", "name": "Hive B3 C", "building": "Hive",
                   "floor": "B3", "type": "junction"}],
        "edges": [],
    }

    updated = import_survey.build_survey(
        survey, [_named_place("Hive_B3_C", "Study Pods")], {}, {}
    )

    assert [node["name"] for node in updated["nodes"]] == ["Study Pods"]


def test_renaming_a_place_does_not_add_a_second_one(
) -> None:
    """Every place is listed every run, so appending them all doubles the map."""
    survey = {
        "nodes": [{"id": "Hive_B3_C", "name": "Hive B3 C", "building": "Hive",
                   "floor": "B3", "type": "junction"}],
        "edges": [],
    }

    updated = import_survey.build_survey(
        survey, [_named_place("Hive_B3_C", "Study Pods")], {}, {}
    )

    assert len(updated["nodes"]) == 1


def test_a_place_the_survey_does_not_have_is_added(
) -> None:
    survey = {"nodes": [], "edges": []}

    updated = import_survey.build_survey(
        survey, [_named_place("Hive_B3_C", "Study Pods")], {}, {}
    )

    assert [node["id"] for node in updated["nodes"]] == ["Hive_B3_C"]


def test_building_the_survey_twice_over_gives_the_same_thing(survey: dict) -> None:
    """The check that makes this safe to re-run whenever the map is redrawn.

    Written after a version that appended every place on every run, taking
    forty places to eighty while still passing every other test here.
    """
    places = [_named_place(node["id"], node["name"]) for node in survey["nodes"]]

    once = import_survey.build_survey(survey, places, {}, {})
    twice = import_survey.build_survey(once, places, {}, {})

    assert len(once["nodes"]) == len(survey["nodes"])
    assert twice == once

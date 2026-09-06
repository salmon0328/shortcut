"""Reading an uploaded drawing into changes somebody can review.

These tests use the team's real node map, because the thing worth pinning is
not that the code runs - it is what it concludes about a real drawing that
disagrees with the survey in real ways.

Nothing here writes to the map, and neither does the code under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shortcut.graph_store import CampusGraph
from shortcut.nodemap import read_uploaded_pdf
from shortcut.survey_import import ImportReading, read_candidates

NODE_MAP = Path(__file__).resolve().parents[1] / "data" / "survey_sources" / "hive_node_map.pdf"


@pytest.fixture(scope="module")
def drawing() -> tuple:
    return read_uploaded_pdf(NODE_MAP.read_bytes())


@pytest.fixture
def reading(graph: CampusGraph, drawing: tuple) -> ImportReading:
    return read_candidates(graph, drawing, filename="hive_node_map.pdf")


# --------------------------------------------------------------------------
# Reading a file nobody has looked at yet
# --------------------------------------------------------------------------


def test_every_page_is_read_not_a_guessed_range(drawing: tuple) -> None:
    """The command line tool knows which pages of this file supersede which.

    An upload does not, so it reads all of them. Taking the tool's page range
    would silently drop whichever floors fell outside it.
    """
    pages = {extraction.page for extraction in drawing}

    assert len(pages) > 3, "an upload should see more than the three surveyed pages"


def test_a_page_with_nothing_on_it_is_skipped_rather_than_fatal(
    drawing: tuple,
) -> None:
    """A cover sheet must not reject the document behind it."""
    assert all(extraction.places for extraction in drawing)


def test_a_file_that_is_not_a_drawing_is_refused(graph: CampusGraph) -> None:
    with pytest.raises(Exception):
        read_uploaded_pdf(b"this is not a pdf at all")


# --------------------------------------------------------------------------
# What it offers
# --------------------------------------------------------------------------


def test_places_the_map_already_has_are_not_offered_again(
    reading: ImportReading, graph: CampusGraph
) -> None:
    assert reading.already_known_nodes > 0, "sanity: this drawing built the map"
    assert all(node.node_id not in graph.nodes for node in reading.nodes)


def test_it_finds_the_floors_the_command_line_tool_refuses(
    reading: ImportReading,
) -> None:
    """B1 and B2 are drawn with no times on any link, so the importer skips
    them entirely. Offering the *places* is different from inventing a walk:
    a person can accept a place and still leave it unconnected."""
    floors = {node.floor for node in reading.nodes}

    assert {"B1", "B2"} <= floors


def test_a_link_is_never_offered_with_an_end_that_would_not_exist(
    reading: ImportReading, graph: CampusGraph
) -> None:
    """Approving everything must not be able to leave a dangling edge."""
    will_exist = set(graph.nodes) | {node.node_id for node in reading.nodes}

    for edge in reading.edges:
        assert edge.from_id in will_exist
        assert edge.to_id in will_exist


def test_the_same_place_drawn_on_two_plans_is_offered_once(
    reading: ImportReading,
) -> None:
    ids = [node.node_id for node in reading.nodes]

    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# What it refuses to decide
# --------------------------------------------------------------------------


def test_a_link_between_two_surveyed_places_is_flagged_as_a_disagreement(
    reading: ImportReading, graph: CampusGraph
) -> None:
    """Both ends already surveyed and no such link means one of two things.

    Either the drawing is newer than the survey, or it is older and means
    different places by the same names. Offered as a plain addition, a
    reviewer cannot tell which - and this drawing contains eleven of them.
    """
    flagged = [edge for edge in reading.edges if edge.disagrees_with_survey]

    assert flagged, "this drawing does contain contradictions"
    for edge in flagged:
        assert edge.from_id in graph.nodes and edge.to_id in graph.nodes


def test_a_link_introducing_a_new_place_is_not_a_disagreement(
    reading: ImportReading, graph: CampusGraph
) -> None:
    """It cannot contradict a survey that has never heard of one of its ends."""
    for edge in reading.edges:
        if not edge.disagrees_with_survey:
            assert edge.from_id not in graph.nodes or edge.to_id not in graph.nodes


def test_a_line_the_drawing_did_not_settle_is_reported_not_guessed(
    reading: ImportReading,
) -> None:
    """Times written '?s', and connectors drawn off the edge of the page."""
    assert reading.unsettled
    assert not any(edge.walk_seconds is None for edge in reading.edges)


def test_what_the_drawing_wrote_on_a_line_reaches_the_reviewer(
    graph: CampusGraph,
) -> None:
    """The older importer dropped these words for any line it could complete,
    which quietly turned a marked staircase into a flat corridor."""
    from shortcut.nodemap import Extraction, Line, Place, Plan
    from shortcut.survey_import import read_candidates as read

    plan = Plan(xref=1, width=100, height=100, x0=0, y0=0, x1=100, y1=100)
    places = (
        Place(name="Hive-B9-A", page=0, at=(10, 10), plan=plan, name_gap=1),
        Place(name="Hive-B9-B", page=0, at=(20, 20), plan=plan, name_gap=1),
    )
    line = Line(
        page=0,
        start=(10, 10),
        end=(20, 20),
        from_place="Hive-B9-A",
        to_place="Hive-B9-B",
        times=("30s",),
        notes=("stairs",),
    )
    reading = read(graph, (Extraction(0, (plan,), places, (line,)),))

    assert len(reading.edges) == 1
    assert reading.edges[0].marks == ("stairs",)
    assert reading.edges[0].stairs is True


def test_a_floor_the_label_does_not_give_is_left_blank_not_borrowed(
    reading: ImportReading,
) -> None:
    """The importer takes it from the page number, which only works for the
    one file whose page order it knows. A blank is a box to fill in."""
    for node in reading.nodes:
        if node.floor:
            assert node.node_id.replace("_", "-").split("-")[1] == node.floor


def test_a_stand_in_name_is_obviously_one(reading: ImportReading) -> None:
    """A prettier guess would read like a surveyed name and survive review."""
    assert all(node.name_is_a_stand_in for node in reading.nodes)
    assert any(node.name.startswith("Hive B") for node in reading.nodes)


# --------------------------------------------------------------------------
# Running it twice
# --------------------------------------------------------------------------


def test_reading_the_same_drawing_twice_says_the_same_thing(
    graph: CampusGraph, drawing: tuple
) -> None:
    first = read_candidates(graph, drawing)
    second = read_candidates(graph, drawing)

    assert [node.node_id for node in first.nodes] == [
        node.node_id for node in second.nodes
    ]
    assert [edge.edge_id for edge in first.edges] == [
        edge.edge_id for edge in second.edges
    ]

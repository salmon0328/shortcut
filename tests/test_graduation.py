"""Tests for folding surveyed changes out of the overrides file into the survey.

Two things are being checked here, and the second matters more than the first:

* the right changes move, and live conditions from reports stay behind
* graduating never changes the map anybody sees, and never leaves the app
  unable to start
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.api import app, get_reports
from shortcut.graph_store import load_graph
from shortcut.overrides import apply_overrides, load_overrides
from shortcut.report_store import ReportStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "graduate_overrides.py"


def load_script(campus_graph: Path, overrides: Path):
    """Import the script with its two file paths pointed at temporary copies.

    The script is a command-line tool, not a package, so it is loaded by path
    and then repointed rather than being run as a subprocess: that keeps the
    assertions on real objects instead of on scraped console output.
    """
    spec = importlib.util.spec_from_file_location("graduate_overrides", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CAMPUS_GRAPH_PATH = campus_graph
    module.GRAPH_OVERRIDES_PATH = overrides
    return module


@pytest.fixture
def workspace(tmp_path: Path, graph_path: Path) -> dict:
    """A throwaway copy of the survey, plus a place to put overrides."""
    campus = tmp_path / "campus_graph.json"
    campus.write_text(graph_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {"campus": campus, "overrides": tmp_path / "graph_overrides.json"}


@pytest.fixture
def surveyed(workspace: dict, tmp_path: Path) -> Iterator[dict]:
    """A realistic session: coordinates surveyed, a place added, a report approved.

    Reports go through a throwaway store, the same way ``test_reports.py``
    does it: without this override, the report submitted below would land in
    the real ``data/reports.json`` on every test run rather than in tmp_path.
    """
    store = ReportStore(tmp_path / "reports.json")
    app.dependency_overrides[get_reports] = lambda: store

    with TestClient(app) as client:
        app.state.overrides_path = workspace["overrides"]
        app.state.reports = store

        client.patch("/admin/nodes/Hive_B5_A", json={"x": 12.5, "y": 30.0})
        client.post(
            "/admin/nodes",
            json={
                "id": "Hive_B5_J",
                "name": "Study Pods",
                "building": "Hive",
                "floor": "B5",
                "type": "junction",
                "x": 50.0,
                "y": 18.0,
                "connections": [
                    {
                        "from_id": "Hive_B5_J",
                        "to_id": "Hive_B5_G",
                        "distance_m": 12,
                        "walk_seconds": 9,
                        "covered": True,
                    }
                ],
            },
        )
        client.post(
            "/reports",
            json={
                "target_kind": "edge",
                "target_id": "Hive_B5_002",
                "condition": "flooded",
                "notes": "burst pipe",
            },
        )
        client.post("/reports/groups/flooded:Hive_B5_002/approve")

        yield {**workspace, "client": client}
    app.dependency_overrides.clear()


def run(workspace: dict, *args: str) -> int:
    """Run the script over the temporary copies, as if from the command line."""
    module = load_script(workspace["campus"], workspace["overrides"])
    import sys

    original = sys.argv
    sys.argv = ["graduate_overrides.py", *args]
    try:
        return module.main()
    finally:
        sys.argv = original


# --------------------------------------------------------------------------
# What moves and what stays
# --------------------------------------------------------------------------


def test_nothing_to_do_when_there_are_no_overrides(workspace: dict) -> None:
    assert run(workspace, "--write") == 0
    assert not workspace["overrides"].exists()


def test_a_dry_run_changes_nothing(surveyed: dict) -> None:
    """The default run only reports, because the file it rewrites is precious."""
    before = surveyed["campus"].read_text(encoding="utf-8")

    assert run(surveyed) == 0

    assert surveyed["campus"].read_text(encoding="utf-8") == before
    assert load_overrides(surveyed["overrides"])["added_nodes"], "overrides untouched"


def test_an_added_place_moves_into_the_survey(surveyed: dict) -> None:
    run(surveyed, "--write")

    survey = json.loads(surveyed["campus"].read_text(encoding="utf-8"))
    assert any(node["id"] == "Hive_B5_J" for node in survey["nodes"])


def test_surveyed_coordinates_move_into_the_survey(surveyed: dict) -> None:
    run(surveyed, "--write")

    survey = json.loads(surveyed["campus"].read_text(encoding="utf-8"))
    node = next(n for n in survey["nodes"] if n["id"] == "Hive_B5_A")
    assert (node["x"], node["y"]) == (12.5, 30.0)


def test_a_report_condition_stays_out_of_the_survey(surveyed: dict) -> None:
    """This week's burst pipe is not a permanent feature of the building."""
    run(surveyed, "--write")

    survey = json.loads(surveyed["campus"].read_text(encoding="utf-8"))
    edge = next(e for e in survey["edges"] if e["id"] == "Hive_B5_002")
    assert edge["blocked"] is False
    assert "condition" not in edge

    remaining = load_overrides(surveyed["overrides"])
    assert remaining["edges"]["Hive_B5_002"] == {
        "blocked": True,
        "condition": "flooded",
    }


def test_conditions_can_be_graduated_on_purpose(surveyed: dict) -> None:
    run(surveyed, "--write", "--include-conditions")

    survey = json.loads(surveyed["campus"].read_text(encoding="utf-8"))
    edge = next(e for e in survey["edges"] if e["id"] == "Hive_B5_002")
    assert edge["blocked"] is True
    assert edge["condition"] == "flooded"


def test_graduated_entries_leave_the_overrides_file(surveyed: dict) -> None:
    """An 'add this place' entry for a place that now exists would break startup."""
    run(surveyed, "--write")

    remaining = load_overrides(surveyed["overrides"])
    assert remaining["added_nodes"] == {}
    assert remaining["added_edges"] == {}
    assert remaining["nodes"] == {}


def test_untouched_entries_keep_their_exact_shape(surveyed: dict) -> None:
    """A readable git diff is the whole point, so nothing else may be reshuffled."""
    before = json.loads(surveyed["campus"].read_text(encoding="utf-8"))
    run(surveyed, "--write")
    after = json.loads(surveyed["campus"].read_text(encoding="utf-8"))

    untouched = {"Hive_B5_B", "Hive_B5_D", "Hive_B5_F"}
    before_by_id = {n["id"]: n for n in before["nodes"]}
    after_by_id = {n["id"]: n for n in after["nodes"]}
    for node_id in untouched:
        assert before_by_id[node_id] == after_by_id[node_id]


# --------------------------------------------------------------------------
# The safety property
# --------------------------------------------------------------------------


def test_the_live_map_is_identical_afterwards(surveyed: dict) -> None:
    """Graduating is a move between files, not a change to what anyone sees."""
    before = apply_overrides(
        load_graph(surveyed["campus"]), load_overrides(surveyed["overrides"])
    )

    run(surveyed, "--write")

    after = apply_overrides(
        load_graph(surveyed["campus"]), load_overrides(surveyed["overrides"])
    )

    assert set(before.nodes) == set(after.nodes)
    assert set(before.edges_by_id) == set(after.edges_by_id)
    for node_id, node in before.nodes.items():
        assert (node.x, node.y, node.name) == (
            after.nodes[node_id].x,
            after.nodes[node_id].y,
            after.nodes[node_id].name,
        )
    for edge_id, edge in before.edges_by_id.items():
        other = after.edges_by_id[edge_id]
        assert (edge.blocked, edge.condition, edge.distance_m) == (
            other.blocked,
            other.condition,
            other.distance_m,
        )


def test_the_app_still_starts_on_the_graduated_files(surveyed: dict) -> None:
    """The bug this guards: a stale 'added' entry stopping the server booting."""
    run(surveyed, "--write")

    graph = apply_overrides(
        load_graph(surveyed["campus"]), load_overrides(surveyed["overrides"])
    )

    assert "Hive_B5_J" in graph.nodes
    assert graph.edge_by_id("Hive_B5_002").blocked is True


def test_running_twice_is_harmless(surveyed: dict) -> None:
    """The second run has nothing left to move, and must not corrupt anything."""
    run(surveyed, "--write")
    after_first = surveyed["campus"].read_text(encoding="utf-8")

    assert run(surveyed, "--write") == 0
    assert surveyed["campus"].read_text(encoding="utf-8") == after_first


# --------------------------------------------------------------------------
# Seeing what is pending, from the app
# --------------------------------------------------------------------------


def test_pending_lists_everything_waiting(surveyed: dict) -> None:
    body = surveyed["client"].get("/admin/pending").json()

    assert body["total"] == 4
    kinds = {(c["change"], c["id"]) for c in body["changes"]}
    assert ("added", "Hive_B5_J") in kinds
    assert ("edited", "Hive_B5_A") in kinds


def test_pending_says_what_would_graduate(surveyed: dict) -> None:
    body = surveyed["client"].get("/admin/pending").json()
    by_id = {c["id"]: c for c in body["changes"]}

    assert by_id["Hive_B5_A"]["graduating_fields"] == ["x", "y"]
    assert by_id["Hive_B5_A"]["live_fields"] == []
    assert by_id["Hive_B5_002"]["graduating_fields"] == []
    assert by_id["Hive_B5_002"]["live_fields"] == ["blocked", "condition"]
    assert body["graduating"] == 3
    assert body["live_only"] == 1


def test_pending_names_places_readably(surveyed: dict) -> None:
    """An id is not something an administrator should have to decode."""
    body = surveyed["client"].get("/admin/pending").json()
    by_id = {c["id"]: c for c in body["changes"]}

    assert by_id["Hive_B5_002"]["label"] == "Lift Lobby → Courtyard"


def test_pending_is_empty_when_nothing_has_changed(workspace: dict) -> None:
    with TestClient(app) as client:
        app.state.overrides_path = workspace["overrides"]
        body = client.get("/admin/pending").json()

    assert body == {"changes": [], "total": 0, "graduating": 0, "live_only": 0}

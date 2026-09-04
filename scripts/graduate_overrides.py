"""Fold surveyed changes out of the overrides file and into the campus graph.

While the app runs, everything you add or change through admin mode lands in
``data/graph_overrides.json``. That file is deliberately *not* committed: it is
this machine's working state. Once a batch of surveying is confirmed correct,
it should graduate into ``data/campus_graph.json``, which is the one map file
under version control - so the work has a commit history, survives a fresh
clone, and reaches teammates when you push.

This script does that one job, and nothing else. It never commits, never
pushes, and never clears the overrides file. What it produces is a changed
``campus_graph.json`` for you to read as a git diff and commit yourself.

Usage, from the repository root::

    python scripts/graduate_overrides.py              # show what would change
    python scripts/graduate_overrides.py --write      # actually change the file
    python scripts/graduate_overrides.py --write --include-conditions

Nothing happens without ``--write``: a run with no flags only reports, because
the file it rewrites is the one thing in ``data/`` that is hard to get back.

Writing removes the graduated entries from the overrides file, and only those.
That is not tidying up, it is part of graduating: an entry that says "add this
place" when the place is already in the survey is stale, and would stop the
app from starting next time. Anything the script did not graduate is left
exactly where it was.

What graduates, and what does not
---------------------------------
Places and links added through admin mode always graduate, as do edits that
describe the building itself: names, floors, coordinates, distances, walking
times, whether something is covered or a staircase.

``blocked`` and ``condition`` do *not*, unless you pass
``--include-conditions``. They describe a passing state of the world that came
from an approved report - a corridor flooded this week, a lobby crowded at
lunch. Writing those into the survey would make this week's puddle a permanent
feature of the map. They stay in the overrides file, where a later report can
clear them.

The safety property
-------------------
Graduating must not change what anybody sees. The script proves that before it
writes: it builds the live map from the old survey plus all overrides, builds
it again from the new survey plus whatever overrides remain, and refuses to
write unless the two are identical.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from shortcut.graph_store import CampusGraph, load_graph  # noqa: E402
from shortcut.overrides import (  # noqa: E402
    LIVE_CONDITION_FIELDS,
    apply_overrides,
    empty_overrides,
    load_overrides,
    save_overrides,
)

CAMPUS_GRAPH_PATH = PROJECT_ROOT / "data" / "campus_graph.json"
GRAPH_OVERRIDES_PATH = PROJECT_ROOT / "data" / "graph_overrides.json"


def _short(path: Path) -> str:
    """A path to print: relative to the repository when it is inside it."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        # Pointed somewhere else entirely, which is fine - just print it whole
        # rather than failing after the work is already done.
        return str(path)


# --------------------------------------------------------------------------
# Deciding what moves
# --------------------------------------------------------------------------


def _fields_that_graduate(
    fields: dict[str, Any], include_conditions: bool
) -> dict[str, Any]:
    """The part of one change that belongs in the survey."""
    if include_conditions:
        return dict(fields)
    return {
        key: value
        for key, value in fields.items()
        if key not in LIVE_CONDITION_FIELDS
    }


def _split(
    overrides: dict[str, dict[str, Any]], include_conditions: bool
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Divide the overrides into what graduates and what stays behind."""
    moving = empty_overrides()
    staying = empty_overrides()

    # Whole new places and links always graduate: they are the building, not a
    # passing state of it.
    moving["added_nodes"] = copy.deepcopy(overrides["added_nodes"])
    moving["added_edges"] = copy.deepcopy(overrides["added_edges"])

    for section in ("nodes", "edges"):
        for target_id, fields in overrides[section].items():
            if not isinstance(fields, dict):
                continue
            graduating = _fields_that_graduate(fields, include_conditions)

            # A node cannot carry "blocked" in the graph file - it is not a
            # field a place has. Its effect is on the corridors around it, and
            # it is transient anyway, so it always stays behind.
            if section == "nodes":
                graduating.pop("blocked", None)

            remaining = {
                key: value for key, value in fields.items() if key not in graduating
            }
            if graduating:
                moving[section][target_id] = graduating
            if remaining:
                staying[section][target_id] = remaining

    return moving, staying


# --------------------------------------------------------------------------
# Rewriting the survey
# --------------------------------------------------------------------------


def _graduated_graph_json(
    survey: dict[str, Any], moving: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The new survey file: the old one with the graduating changes folded in.

    Built by editing the raw JSON rather than re-serialising the loaded graph,
    so untouched entries keep their exact original shape and the git diff shows
    only what actually changed.
    """
    updated = copy.deepcopy(survey)

    for node in updated["nodes"]:
        node.update(moving["nodes"].get(node["id"], {}))
    for edge in updated["edges"]:
        edge.update(moving["edges"].get(edge["id"], {}))

    for node_id, fields in moving["added_nodes"].items():
        updated["nodes"].append({"id": node_id, **fields})
    for edge_id, fields in moving["added_edges"].items():
        updated["edges"].append({"id": edge_id, **fields})

    return updated


def _same_map(before: CampusGraph, after: CampusGraph) -> list[str]:
    """Every way the two maps differ. Empty means graduating changed nothing."""
    problems: list[str] = []

    if set(before.nodes) != set(after.nodes):
        missing = set(before.nodes) - set(after.nodes)
        extra = set(after.nodes) - set(before.nodes)
        problems.append(f"places differ (missing {missing or '-'}, extra {extra or '-'})")

    for node_id, node in before.nodes.items():
        other = after.nodes.get(node_id)
        if other is None:
            continue
        for field in ("name", "building", "floor", "type", "x", "y", "condition"):
            if getattr(node, field) != getattr(other, field):
                problems.append(
                    f"{node_id}.{field}: {getattr(node, field)!r} -> "
                    f"{getattr(other, field)!r}"
                )

    if set(before.edges_by_id) != set(after.edges_by_id):
        problems.append("links differ")

    for edge_id, edge in before.edges_by_id.items():
        other = after.edges_by_id.get(edge_id)
        if other is None:
            continue
        for field in (
            "from_id", "to_id", "distance_m", "walk_seconds", "covered", "stairs",
            "lift", "shuttle", "wait_seconds", "blocked", "one_way", "condition",
            "directions_forward", "directions_reverse",
        ):
            if getattr(edge, field) != getattr(other, field):
                problems.append(
                    f"{edge_id}.{field}: {getattr(edge, field)!r} -> "
                    f"{getattr(other, field)!r}"
                )

    return problems


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _describe(moving: dict, staying: dict) -> None:
    counts = {section: len(moving[section]) for section in moving}
    total_moving = sum(counts.values())
    total_staying = sum(len(staying[section]) for section in staying)

    if total_moving == 0:
        print("Nothing to graduate: no surveyed changes are waiting.")
    else:
        print(f"Graduating {total_moving} change(s) into the survey:")
        for node_id, fields in moving["added_nodes"].items():
            print(f"  + place  {node_id}  ({fields.get('name', 'unnamed')})")
        for edge_id, fields in moving["added_edges"].items():
            print(f"  + link   {edge_id}  ({fields.get('from')} -> {fields.get('to')})")
        for node_id, fields in moving["nodes"].items():
            print(f"  ~ place  {node_id}  {', '.join(sorted(fields))}")
        for edge_id, fields in moving["edges"].items():
            print(f"  ~ link   {edge_id}  {', '.join(sorted(fields))}")

    if total_staying:
        print(f"\nLeaving {total_staying} live condition(s) in the overrides file:")
        for section in ("nodes", "edges"):
            for target_id, fields in staying[section].items():
                print(f"  · {section[:-1]:5} {target_id}  {', '.join(sorted(fields))}")
        print("  (these came from reports and describe now, not the building)")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fold surveyed changes into data/campus_graph.json."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually rewrite the survey file. Without this, only report.",
    )
    parser.add_argument(
        "--include-conditions",
        action="store_true",
        help="Also graduate blocked/condition flags from approved reports.",
    )
    args = parser.parse_args()

    if not GRAPH_OVERRIDES_PATH.exists():
        print(f"No overrides file at {GRAPH_OVERRIDES_PATH}.")
        print("Nothing has been changed since the survey, so there is nothing to do.")
        return 0

    survey = json.loads(CAMPUS_GRAPH_PATH.read_text(encoding="utf-8"))
    overrides = load_overrides(GRAPH_OVERRIDES_PATH)
    moving, staying = _split(overrides, args.include_conditions)

    _describe(moving, staying)
    if sum(len(moving[section]) for section in moving) == 0:
        return 0

    updated = _graduated_graph_json(survey, moving)

    # Prove the live map is unchanged before touching anything. The new survey
    # plus whatever overrides remain must give exactly the map the old survey
    # plus all the overrides gives today.
    with tempfile.TemporaryDirectory() as work:
        candidate_path = Path(work) / "candidate.json"
        candidate_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")

        try:
            candidate = load_graph(candidate_path)
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            print(f"\nRefusing to write: the result is not a valid graph.\n  {error}")
            return 1

        before = apply_overrides(load_graph(CAMPUS_GRAPH_PATH), overrides)
        # Only what stays behind: the graduated entries are in the survey now,
        # and an "add this place" entry for a place that already exists would
        # stop the app from starting.
        after = apply_overrides(candidate, staying)

        differences = _same_map(before, after)
        if differences:
            print("\nRefusing to write: graduating would change the live map.")
            for line in differences[:10]:
                print(f"  {line}")
            return 1

    print(
        f"\nChecked: {len(before.nodes)} places and {len(before.edges)} links, "
        f"map unchanged."
    )

    if not args.write:
        print("\nThis was a dry run. Re-run with --write to apply it.")
        return 0

    CAMPUS_GRAPH_PATH.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {_short(CAMPUS_GRAPH_PATH)}.")

    save_overrides(GRAPH_OVERRIDES_PATH, staying)
    remaining = sum(len(staying[section]) for section in staying)
    print(
        f"Removed the graduated entries from "
        f"{_short(GRAPH_OVERRIDES_PATH)}"
        + (f"; {remaining} live condition(s) left in place." if remaining else ".")
    )

    print("\nNext: review with `git diff data/campus_graph.json`, then commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

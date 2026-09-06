"""Tests for the Verifier: the one component here that changes the map itself.

Everything runs offline against a :class:`MockLlm`, so the suite never calls
Bedrock and never spends anything. That matters more here than elsewhere - a
test that needed a model to decide whether a corridor closes would be a test
nobody runs.

The rule these tests exist to defend: **the model rates, the code decides.**
Several of them hand the model a reading that argues loudly for approval and
check that the arithmetic still refuses it, because the report notes are text
typed by strangers and this agent writes to a map everybody routes over.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortcut.ai.bedrock import MockLlm
from shortcut.ai.impact import assess
from shortcut.ai.prompts import VERIFY_VERSION
from shortcut.ai.routes import get_llm
from shortcut.ai.schemas import ReportReading, ReportWeight
from shortcut.ai.verifier import LONG_DETOUR_SECONDS, verify_group
from shortcut.api import app, get_reports
from shortcut.graph_store import load_graph
from shortcut.report_store import ReportStore

# A corridor with a way round it, so closing it strands nobody. Its detour is
# a few seconds, which is what makes it the "ordinary" case.
ORDINARY_EDGE = "Hive_B5_002"
# One where the way round is about four minutes - far enough to raise the bar.
LONG_DETOUR_EDGE = "Hive_B3_F--Hive_B3_G"
# The one cut vertex in the surveyed map: closing SS_B3_D leaves SS_B3_E with
# no way in at all. Every "would strand somewhere" test uses this, and
# tests/test_report_verifier.py::test_the_map_still_has_exactly_one_cut_vertex
# fails loudly if a resurvey ever makes that untrue.
CUT_VERTEX = "SS_B3_D"
STRANDED_BY_IT = "SS_B3_E"
CROWDABLE_NODE = "Hive_B5_G"


@pytest.fixture(scope="module")
def graph():
    return load_graph("data/campus_graph.json")


@pytest.fixture
def store(tmp_path: Path) -> ReportStore:
    return ReportStore(tmp_path / "reports.json")


def reading(*weights: float, describes: bool = True, contradiction: str = "") -> ReportReading:
    """A model answer that rates submissions in the order they were given."""
    return ReportReading(
        weights=[
            ReportWeight(report_id=f"r{index}", weight=weight, why="because")
            for index, weight in enumerate(weights)
        ],
        describes_condition=describes,
        summary="a summary",
        contradiction=contradiction,
    )


def group_of(store: ReportStore, key: str):
    return next(group for group in store.pending_groups() if group.key == key)


def file_reports(
    store: ReportStore, count: int, *, target_kind="edge", target_id=ORDINARY_EDGE,
    condition="blocked",
):
    """Put ``count`` submissions in the store, and hand back the group."""
    for index in range(count):
        store.add(
            target_kind=target_kind,
            target_id=target_id,
            condition=condition,
            notes=f"note {index}",
        )
    key = f"{condition}:{target_id}"
    return group_of(store, key), [
        report for report in store.all(status="pending") if report.group_key == key
    ]


def judge(graph, store, llm_reading, **kwargs):
    """Run the agent over a freshly filed group, with a canned model answer."""
    count = kwargs.pop("count", 3)
    group, reports = file_reports(store, count, **kwargs)
    # The canned reading rates by position; re-key it onto the real report ids.
    fixed = ReportReading(
        weights=[
            ReportWeight(report_id=report.id, weight=weight.weight, why=weight.why)
            for report, weight in zip(reports, llm_reading.weights)
        ],
        describes_condition=llm_reading.describes_condition,
        summary=llm_reading.summary,
        contradiction=llm_reading.contradiction,
    )
    llm = MockLlm({VERIFY_VERSION: fixed})
    return verify_group(graph, llm, group, reports, "somewhere")


# --------------------------------------------------------------------------
# What closing something would cost
# --------------------------------------------------------------------------


def test_an_advisory_condition_closes_nothing(graph) -> None:
    """"Crowded" warns. A warning cannot strand anybody, so nothing is priced."""
    impact = assess(
        graph, target_kind="node", target_id=CROWDABLE_NODE, condition="crowded"
    )

    assert impact.blocks_routes is False
    assert impact.cut_off == ()


def test_closing_a_corridor_with_a_way_round_strands_nobody(graph) -> None:
    impact = assess(
        graph, target_kind="edge", target_id=ORDINARY_EDGE, condition="blocked"
    )

    assert impact.blocks_routes is True
    assert impact.cut_off == ()
    # There is a way round, so there is a number for how much longer it is.
    assert impact.detour_seconds is not None


def test_closing_the_cut_vertex_strands_the_place_behind_it(graph) -> None:
    impact = assess(
        graph, target_kind="node", target_id=CUT_VERTEX, condition="blocked"
    )

    assert impact.cut_off == (STRANDED_BY_IT,)
    # The reported place is closed on purpose, so it is not collateral.
    assert CUT_VERTEX not in impact.cut_off


def test_the_map_still_has_exactly_one_cut_vertex(graph) -> None:
    """A guard on the fixture above, not on the code.

    Every escalation test leans on ``SS_B3_D`` being the one place whose
    closure strands somewhere. A resurvey that adds a second way into
    ``SS_B3_E`` would leave those tests passing for the wrong reason - they
    would stop testing escalation and nobody would notice - so the assumption
    is asserted where a change to it fails loudly and says what to fix.
    """
    strands = {
        node_id
        for node_id in graph.nodes
        if assess(
            graph, target_kind="node", target_id=node_id, condition="blocked"
        ).cut_off
    }

    assert strands == {CUT_VERTEX}, (
        "The surveyed map's connectivity changed. Pick a new CUT_VERTEX and "
        "STRANDED_BY_IT from this set, or from the edges if it is now empty."
    )


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------


def test_well_corroborated_and_harmless_is_approved(graph, store) -> None:
    verdict = judge(graph, store, reading(1.0, 1.0, 0.5))

    assert verdict.action == "approved"
    assert verdict.weight == 2.5
    assert verdict.threshold == 2.0


def test_closing_something_needs_more_than_warning_about_it(graph, store) -> None:
    """The same corroboration, two conditions, two outcomes.

    This is the whole content of the two threshold constants: a wrongly
    closed corridor sends somebody the long way round in the rain, a wrongly
    flagged busy lobby costs them nothing.
    """
    weights = reading(1.0, 0.5)

    advisory = judge(
        graph, store, weights, count=2,
        target_kind="node", target_id=CROWDABLE_NODE, condition="crowded",
    )
    blocking = judge(graph, store, weights, count=2)

    assert advisory.action == "approved"
    assert advisory.threshold == 1.0
    assert blocking.action == "escalated"
    assert blocking.threshold == 2.0


def test_stranding_a_place_is_escalated_however_many_people_agree(graph, store) -> None:
    """The threshold the agent cannot move.

    Five people, every one of them rated fully credible - far past any bar in
    the config - and it still goes to a person, because no amount of
    corroboration should be able to punch a hole in the map.
    """
    verdict = judge(
        graph, store, reading(1.0, 1.0, 1.0, 1.0, 1.0), count=5,
        target_kind="node", target_id=CUT_VERTEX,
    )

    assert verdict.action == "escalated"
    assert verdict.weight == 5.0
    assert verdict.weight > verdict.threshold, "cleared the bar and was held anyway"
    assert STRANDED_BY_IT in verdict.impact.cut_off
    assert STRANDED_BY_IT in verdict.why


def test_nothing_worth_acting_on_is_rejected_outright(graph, store) -> None:
    """Junk does not need a human to adjudicate it."""
    verdict = judge(graph, store, reading(0.0, 0.0, 0.0))

    assert verdict.action == "rejected"
    assert verdict.weight == 0.0


def test_too_few_voices_waits_rather_than_rejecting(graph, store) -> None:
    """Not enough corroboration is a reason to wait, not to decide it is false.

    Rejecting would close a queue entry that tomorrow's reporter would have
    completed.
    """
    verdict = judge(graph, store, reading(0.6, 0.4))

    assert verdict.action == "escalated"
    assert "Waiting" in verdict.why


def test_submissions_that_disagree_go_to_a_person(graph, store) -> None:
    verdict = judge(
        graph, store,
        reading(1.0, 1.0, 1.0, contradiction="one says shut, one walked through"),
    )

    assert verdict.action == "escalated"
    assert "walked through" in verdict.why


def test_the_wrong_kind_of_problem_is_escalated_not_rejected(graph, store) -> None:
    """Reported as blocked, described as merely busy.

    Rejecting would throw away something that may well be true. A person can
    re-file it under the condition it actually describes.
    """
    verdict = judge(graph, store, reading(1.0, 1.0, 1.0, describes=False))

    assert verdict.action == "escalated"
    assert "re-file" in verdict.why


def test_a_long_way_round_raises_the_bar(graph, store) -> None:
    """A closure with a four-minute detour behind it is worth one more voice.

    The same two credible reports that would have carried an ordinary
    corridor do not carry this one, because the cost of being wrong about it
    is a walk round the outside of a building rather than through the next
    door along.
    """
    ordinary = judge(graph, store, reading(1.0, 1.0), count=2)
    long_way = judge(
        graph, store, reading(1.0, 1.0), count=2, target_id=LONG_DETOUR_EDGE
    )

    assert ordinary.raised_because == ""
    assert ordinary.action == "approved"

    assert long_way.raised_because, "a 249s detour should have raised the bar"
    assert long_way.threshold == long_way.base_threshold + 1.0
    assert long_way.action == "escalated"


def test_the_detour_that_raises_the_bar_is_still_long(graph) -> None:
    """A guard on the fixture, like the cut-vertex one above.

    If a resurvey opens a shortcut beside this corridor, the test above stops
    testing the raise and starts passing for no reason.
    """
    impact = assess(
        graph, target_kind="edge", target_id=LONG_DETOUR_EDGE, condition="blocked"
    )

    assert impact.detour_seconds is not None
    assert impact.detour_seconds >= LONG_DETOUR_SECONDS, (
        "This corridor now has a quicker way round. Pick a new "
        "LONG_DETOUR_EDGE from whatever is still slow to walk around."
    )


# --------------------------------------------------------------------------
# Weights are counted honestly
# --------------------------------------------------------------------------


def test_a_repeated_id_cannot_inflate_the_total(graph, store) -> None:
    """A model returning the same id twice must not double-count it.

    The notes are attacker-supplied text and the ids go into the prompt, so
    this is the shape of a real attempt to talk past the threshold.
    """
    group, reports = file_reports(store, 2)
    duplicated = ReportReading(
        weights=[
            ReportWeight(report_id=reports[0].id, weight=1.0, why="one"),
            ReportWeight(report_id=reports[0].id, weight=1.0, why="again"),
            ReportWeight(report_id=reports[0].id, weight=1.0, why="and again"),
        ],
        describes_condition=True,
        summary="",
    )
    llm = MockLlm({VERIFY_VERSION: duplicated})

    verdict = verify_group(graph, llm, group, reports, "somewhere")

    # One rated submission, one unrated: 1.0, not 3.0.
    assert verdict.weight == 1.0
    assert verdict.action == "escalated"


def test_an_unrated_submission_counts_as_nothing(graph, store) -> None:
    """Silence about a report holds it back rather than pushing it through."""
    group, reports = file_reports(store, 3)
    partial = ReportReading(
        weights=[ReportWeight(report_id=reports[0].id, weight=1.0, why="one")],
        describes_condition=True,
        summary="",
    )

    verdict = verify_group(
        graph, MockLlm({VERIFY_VERSION: partial}), group, reports, "somewhere"
    )

    assert verdict.weight == 1.0


def test_an_invented_id_counts_as_nothing(graph, store) -> None:
    group, reports = file_reports(store, 2)
    invented = ReportReading(
        weights=[
            ReportWeight(report_id="not-a-real-report", weight=1.0, why="?"),
            ReportWeight(report_id=reports[0].id, weight=0.5, why="real"),
        ],
        describes_condition=True,
        summary="",
    )

    verdict = verify_group(
        graph, MockLlm({VERIFY_VERSION: invented}), group, reports, "somewhere"
    )

    assert verdict.weight == 0.5


# --------------------------------------------------------------------------
# Through the endpoints
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, store: ReportStore) -> Iterator[TestClient]:
    app.dependency_overrides[get_reports] = lambda: store
    with TestClient(app) as test_client:
        app.state.reports = store
        app.state.overrides_path = tmp_path / "graph_overrides.json"
        yield test_client
    app.dependency_overrides.clear()


def with_llm(answer: ReportReading):
    app.dependency_overrides[get_llm] = lambda: MockLlm({VERIFY_VERSION: answer})


def submit(client: TestClient, count: int, **overrides) -> str:
    payload = {
        "target_kind": "edge",
        "target_id": ORDINARY_EDGE,
        "condition": "blocked",
        "notes": "barriers across it, signage says closed until Friday",
    }
    payload.update(overrides)
    for _ in range(count):
        client.post("/reports", json=payload)
    return f"{payload['condition']}:{payload['target_id']}"


def rate_everything(client: TestClient, key: str, weight: float) -> None:
    """Point the mock at the real report ids for the group in ``key``."""
    pending = [r for r in client.get("/reports?report_status=pending").json()]
    with_llm(
        ReportReading(
            weights=[
                ReportWeight(report_id=r["id"], weight=weight, why="because")
                for r in pending
            ],
            describes_condition=True,
            summary="",
        )
    )


def test_approving_through_the_agent_changes_the_map(client) -> None:
    """And changes it the same way a person's click does.

    Not a separate write path: the verdict is handed to the same
    ``_review_group`` the Approve button calls, so there is one way for the
    map to change rather than two that can drift apart.
    """
    key = submit(client, 3)
    rate_everything(client, key, 1.0)

    verdict = client.post(f"/reports/groups/{key}/verify").json()

    assert verdict["action"] == "approved"
    assert verdict["applied"] is True
    # Gone from the queue, and every submission in it is settled.
    assert key not in [g["key"] for g in client.get("/reports/groups").json()]
    assert client.get("/reports?report_status=approved").json() != []


def test_an_escalated_report_stays_in_the_queue(client) -> None:
    """The agent declining to decide must leave the work where a person finds it."""
    key = submit(client, 3, target_kind="node", target_id=CUT_VERTEX)
    rate_everything(client, key, 1.0)

    verdict = client.post(f"/reports/groups/{key}/verify").json()

    assert verdict["action"] == "escalated"
    assert verdict["applied"] is False
    assert key in [g["key"] for g in client.get("/reports/groups").json()]


def test_a_verdict_carries_the_arithmetic_behind_it(client) -> None:
    """An agent that writes to the map has to be answerable for it afterwards.

    A reviewer disagreeing with an outcome needs to see which step they
    disagree with, so every number that went into it is in the response.
    """
    key = submit(client, 2)
    rate_everything(client, key, 0.5)

    verdict = client.post(f"/reports/groups/{key}/verify").json()

    assert verdict["weight"] == 1.0
    assert verdict["threshold"] == 2.0
    assert verdict["base_threshold"] == 2.0
    assert verdict["impact"]["blocks_routes"] is True
    assert verdict["impact"]["describes"]
    assert len(verdict["reading"]["weights"]) == 2
    assert verdict["why"]


def test_verifying_something_that_is_not_waiting_is_a_404(client) -> None:
    with_llm(reading(1.0))

    response = client.post("/reports/groups/blocked:nothing-here/verify")

    assert response.status_code == 404


def test_an_unreachable_model_says_so_rather_than_deciding(client) -> None:
    """503, and the map untouched.

    The failure mode that matters: a model that cannot be reached must not
    become a report that quietly went through, or one that was quietly
    thrown away.
    """
    key = submit(client, 3)
    # A mock with nothing registered raises on every call.
    app.dependency_overrides[get_llm] = lambda: MockLlm()

    response = client.post(f"/reports/groups/{key}/verify")

    assert response.status_code == 503
    assert key in [g["key"] for g in client.get("/reports/groups").json()]


def test_working_the_whole_queue_settles_what_it_can(client) -> None:
    """One call per problem, and the ones it will not decide stay put."""
    settles = submit(client, 3)
    holds = submit(client, 3, target_kind="node", target_id=CUT_VERTEX)
    rate_everything(client, settles, 1.0)

    verdicts = client.post("/reports/verify-all").json()

    by_key = {v["key"]: v for v in verdicts}
    assert by_key[settles]["applied"] is True
    assert by_key[holds]["applied"] is False
    assert [g["key"] for g in client.get("/reports/groups").json()] == [holds]


def test_the_suite_cannot_reach_a_real_model_whatever_the_developer_set() -> None:
    """A guard on the test setup itself, not on any feature.

    Two components call models now, and both resolve their own settings from
    ``.env`` rather than from anything the app hands them. So a developer who
    has switched ``MOCK_MODE=false`` on to use the feature - which is the
    whole point of the setting - would otherwise have the suite quietly making
    real calls: slower, chargeable, and impossible on a train. ``conftest.py``
    forces it back, and this is what fails if that ever stops working.
    """
    from shortcut.ai.config import load_settings

    assert load_settings().mock_mode is True


def test_the_offline_reader_can_work_the_queue_too(client) -> None:
    """No AWS, no network, and the feature still does something honest.

    It rates conservatively - nothing it produces reaches 1.0 - so the
    consequential reports pile up for a person rather than going through on
    a stand-in's say-so. That is the intended behaviour, not a shortcoming.
    """
    from shortcut.ai.offline import KeywordLlm

    key = submit(client, 3)
    app.dependency_overrides[get_llm] = lambda: KeywordLlm()

    verdict = client.post(f"/reports/groups/{key}/verify").json()

    assert verdict["action"] in {"approved", "escalated", "rejected"}
    assert verdict["reading"]["weights"], "it rated the submissions"
    assert all(w["weight"] <= 0.8 for w in verdict["reading"]["weights"])

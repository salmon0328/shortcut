"""Storage for problems reported by users.

A report says "something is wrong at this place": a blocked corridor, a
flooded stairwell, a crowded lobby. Reports live in their own JSON file rather
than in ``data/campus_graph.json``, because the graph is hand-surveyed source
data and should never be rewritten by whatever a user typed into a form.

Every report is kept as its own record, for good. Several people reporting the
same problem produces several rows, not one merged row: the count shown to an
administrator is worked out by grouping at read time
(:meth:`ReportStore.pending_groups`). That way the raw submissions stay
available for checking what people actually sent.

Reports are read from and written to disk on every call. That is slower than
caching, but it keeps a reloading dev server and a running one from disagreeing
about what has been submitted. Nothing here calls AI, AWS or the network.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "Condition",
    "CONDITIONS",
    "ROUTE_BLOCKING_CONDITIONS",
    "Report",
    "ReportGroup",
    "ReportStore",
    "ReportStoreError",
    "TargetKind",
    "ReportStatus",
    "group_key_for",
]


# What can be wrong with a place.
Condition = Literal["construction", "blocked", "flooded", "crowded"]
CONDITIONS: tuple[Condition, ...] = ("construction", "blocked", "flooded", "crowded")

# Which of those stop a route being planned through somewhere, once approved.
# "crowded" is deliberately absent: a busy corridor is worth warning about, but
# it is still walkable, and routing around it would send people the long way
# for no real reason. Change this set to change that policy.
ROUTE_BLOCKING_CONDITIONS: frozenset[str] = frozenset(
    {"construction", "blocked", "flooded"}
)

# A report is about a place (a node) or a stretch between two places (an edge).
TargetKind = Literal["node", "edge"]

ReportStatus = Literal["pending", "approved", "rejected"]


class ReportStoreError(Exception):
    """The reports file is missing, unreadable, or not shaped like reports."""


def _now() -> str:
    """The current time, as an ISO 8601 string in UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def group_key_for(condition: str, target_id: str) -> str:
    """The id for the group a report belongs to.

    Two reports are "the same problem" when they name the same place and the
    same condition. That is all the matching this does, and it is enough
    because a report can only ever name a place already in the graph: the form
    offers a list to choose from rather than a free-text box, so there is no
    spelling to reconcile.
    """
    return f"{condition}:{target_id}"


@dataclass(frozen=True)
class Report:
    """One submission from one person."""

    id: str
    target_kind: TargetKind
    target_id: str
    condition: Condition
    notes: str
    status: ReportStatus
    submitted_at: str
    reviewed_at: str | None = None
    #: A photograph filed with this submission, if the reporter took one.
    #: Evidence of the problem at a moment, not a picture of the place - see
    #: :data:`shortcut.photo_store.PhotoKind`.
    photo_id: str | None = None

    @property
    def group_key(self) -> str:
        return group_key_for(self.condition, self.target_id)


@dataclass(frozen=True)
class ReportGroup:
    """Every pending report about the same problem, gathered for review."""

    key: str
    target_kind: TargetKind
    target_id: str
    condition: Condition
    confirmations: int
    report_ids: tuple[str, ...]
    notes: tuple[str, ...]
    #: Photos filed with these reports, oldest first. Evidence of the problem
    #: rather than pictures of the place, which is why they are carried on the
    #: group instead of looked up from the place's own photos.
    photo_ids: tuple[str, ...] = ()
    first_submitted_at: str = ""
    latest_submitted_at: str = ""

    @property
    def blocks_routes(self) -> bool:
        """Whether approving this group would close the place to routing."""
        return self.condition in ROUTE_BLOCKING_CONDITIONS


class ReportStore:
    """Reads and writes the reports JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- reading -----------------------------------------------------------

    def all(self, status: ReportStatus | None = None) -> list[Report]:
        """Every report, newest last. Optionally only those in one state."""
        reports = self._read()
        if status is None:
            return reports
        return [report for report in reports if report.status == status]

    def get(self, report_id: str) -> Report | None:
        for report in self._read():
            if report.id == report_id:
                return report
        return None

    def pending_groups(self) -> list[ReportGroup]:
        """Pending reports gathered into one entry per problem.

        Ordered by how many people reported the problem, most-confirmed first,
        so the queue leads with whatever is best corroborated.
        """
        grouped: dict[str, list[Report]] = {}
        for report in self._read():
            if report.status != "pending":
                continue
            grouped.setdefault(report.group_key, []).append(report)

        groups = [
            self._make_group(key, members) for key, members in grouped.items()
        ]
        groups.sort(key=lambda group: (-group.confirmations, group.key))
        return groups

    # -- writing -----------------------------------------------------------

    def add(
        self,
        *,
        target_kind: TargetKind,
        target_id: str,
        condition: Condition,
        notes: str = "",
        photo_id: str | None = None,
    ) -> Report:
        """Record one new report, always as a new row."""
        report = Report(
            id=uuid.uuid4().hex,
            target_kind=target_kind,
            target_id=target_id,
            condition=condition,
            notes=notes,
            status="pending",
            submitted_at=_now(),
            photo_id=photo_id,
        )

        reports = self._read()
        reports.append(report)
        self._write(reports)
        return report

    def set_group_status(self, key: str, status: ReportStatus) -> list[Report]:
        """Approve or reject every pending report in one group.

        Returns the reports that changed, which is empty when the group has
        already been dealt with or never existed.
        """
        reports = self._read()
        reviewed_at = _now()
        changed: list[Report] = []

        for index, report in enumerate(reports):
            if report.status != "pending" or report.group_key != key:
                continue
            updated = replace(report, status=status, reviewed_at=reviewed_at)
            reports[index] = updated
            changed.append(updated)

        if changed:
            self._write(reports)
        return changed

    # -- disk --------------------------------------------------------------

    def _read(self) -> list[Report]:
        if not self.path.exists():
            # No file yet simply means nobody has reported anything.
            return []

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ReportStoreError(
                f"{self.path} is not valid JSON: {error.msg} "
                f"(line {error.lineno}, column {error.colno})."
            ) from error
        except OSError as error:
            raise ReportStoreError(f"Could not read {self.path}: {error}") from error

        if not isinstance(raw, list):
            raise ReportStoreError(
                f"{self.path}: expected a list of reports, got {type(raw).__name__}."
            )

        return [self._parse(entry, index) for index, entry in enumerate(raw)]

    def _write(self, reports: list[Report]) -> None:
        payload = [asdict(report) for report in reports]
        text = json.dumps(payload, indent=2) + "\n"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the real file and swap it in, so an interrupted write
        # cannot leave a half-written reports file behind.
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.path)

    @staticmethod
    def _parse(entry: Any, index: int) -> Report:
        if not isinstance(entry, dict):
            raise ReportStoreError(
                f"reports[{index}]: expected an object, got {type(entry).__name__}."
            )
        try:
            return Report(
                id=entry["id"],
                target_kind=entry["target_kind"],
                target_id=entry["target_id"],
                condition=entry["condition"],
                notes=entry.get("notes", ""),
                status=entry.get("status", "pending"),
                submitted_at=entry["submitted_at"],
                reviewed_at=entry.get("reviewed_at"),
                photo_id=entry.get("photo_id"),
            )
        except KeyError as error:
            raise ReportStoreError(
                f"reports[{index}]: missing required field {error.args[0]!r}."
            ) from error

    @staticmethod
    def _make_group(key: str, members: list[Report]) -> ReportGroup:
        ordered = sorted(members, key=lambda report: report.submitted_at)
        first = ordered[0]
        return ReportGroup(
            key=key,
            target_kind=first.target_kind,
            target_id=first.target_id,
            condition=first.condition,
            confirmations=len(ordered),
            report_ids=tuple(report.id for report in ordered),
            notes=tuple(report.notes for report in ordered if report.notes.strip()),
            photo_ids=tuple(
                report.photo_id for report in ordered if report.photo_id
            ),
            first_submitted_at=first.submitted_at,
            latest_submitted_at=ordered[-1].submitted_at,
        )

"""Changes read out of a drawing, waiting for somebody to look at them.

An upload and the decision about it are two different visits to the site, so
what was read has to survive in between. This is where it waits.

Deliberately *not* the overrides file. Everything in ``graph_overrides.json``
is live - :func:`shortcut.overrides.apply_overrides` folds it into the map
that answers every route request - and a place read off a drawing nobody has
checked has no business routing anybody anywhere. So a candidate sits here,
inert, until a reviewer approves it; approving is the moment it moves into
the overrides file and becomes real. Rejecting deletes it, and because it was
never live there is nothing to undo.

The file is this machine's working state, like reports and overrides, and is
not committed.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "Candidate",
    "CandidateKind",
    "CandidateStore",
    "CandidateStoreError",
]

CandidateKind = Literal["node", "edge"]


class CandidateStoreError(Exception):
    """The candidate file is unreadable, or cannot be written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Candidate:
    """One place or link read from a drawing, and not yet decided on."""

    id: str
    kind: CandidateKind
    #: The id it would take in the map: "Hive_B3_C", or "A--B" for a link.
    target_id: str
    #: Everything the reviewer can change before approving, in the shape the
    #: overrides file wants. Held loose rather than typed per kind because a
    #: node and an edge have entirely different fields and this store does not
    #: need to know either set - the API validates them.
    fields: dict[str, Any]
    #: Which file and page it came from, so a reviewer can go and look.
    source: str = ""
    #: What the drawing wrote on a link, if anything.
    marks: tuple[str, ...] = ()
    #: True when this link joins two places the map already has and has no
    #: link between. See EdgeCandidate.disagrees_with_survey.
    disagrees_with_survey: bool = False
    #: True when the name is a stand-in generated from the drawn label.
    name_is_a_stand_in: bool = False
    found_at: str = field(default_factory=_now)

    def with_fields(self, changes: dict[str, Any]) -> "Candidate":
        """A copy carrying a reviewer's corrections."""
        return replace(self, fields={**self.fields, **changes})


class CandidateStore:
    """Reads and writes the candidates waiting for review."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- reading -----------------------------------------------------------

    def all(self) -> list[Candidate]:
        return self._read()

    def get(self, candidate_id: str) -> Candidate | None:
        for candidate in self._read():
            if candidate.id == candidate_id:
                return candidate
        return None

    def target_ids(self) -> set[str]:
        """What is already waiting, so one upload cannot queue it twice."""
        return {candidate.target_id for candidate in self._read()}

    # -- writing -----------------------------------------------------------

    def add_many(self, candidates: list[dict[str, Any]]) -> list[Candidate]:
        """Queue a batch, skipping anything already waiting for the same place.

        Uploading the same drawing twice is an ordinary thing to do - a
        reviewer gets halfway and comes back - and it should not double every
        row waiting for them.
        """
        existing = self._read()
        waiting = {candidate.target_id for candidate in existing}

        added: list[Candidate] = []
        for entry in candidates:
            if entry["target_id"] in waiting:
                continue
            candidate = Candidate(id=uuid.uuid4().hex, **entry)
            waiting.add(candidate.target_id)
            added.append(candidate)

        if added:
            self._write(existing + added)
        return added

    def replace_fields(
        self, candidate_id: str, changes: dict[str, Any]
    ) -> Candidate | None:
        """Apply a reviewer's corrections without approving anything."""
        candidates = self._read()
        for index, candidate in enumerate(candidates):
            if candidate.id != candidate_id:
                continue
            updated = candidate.with_fields(changes)
            candidates[index] = updated
            self._write(candidates)
            return updated
        return None

    def mark_named(self, candidate_id: str) -> Candidate | None:
        """Record that a person has named this place themselves.

        The stand-in flag is what the review screen uses to say "nobody has
        named this yet", so it has to stop being true the moment somebody
        does - otherwise the list keeps nagging about a name it was given.
        """
        candidates = self._read()
        for index, candidate in enumerate(candidates):
            if candidate.id != candidate_id:
                continue
            updated = replace(candidate, name_is_a_stand_in=False)
            candidates[index] = updated
            self._write(candidates)
            return updated
        return None

    def remove(self, candidate_id: str) -> bool:
        """Take one off the queue. Returns whether there was one."""
        candidates = self._read()
        remaining = [c for c in candidates if c.id != candidate_id]
        if len(remaining) == len(candidates):
            return False
        self._write(remaining)
        return True

    def clear(self) -> int:
        """Empty the queue. Returns how many were waiting."""
        waiting = len(self._read())
        self._write([])
        return waiting

    # -- storage -----------------------------------------------------------

    def _read(self) -> list[Candidate]:
        if not self.path.exists():
            return []

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise CandidateStoreError(
                f"{self.path} is not valid JSON: {error.msg} "
                f"(line {error.lineno}, column {error.colno})."
            ) from error
        except OSError as error:
            raise CandidateStoreError(f"Could not read {self.path}: {error}") from error

        if not isinstance(raw, list):
            raise CandidateStoreError(
                f"{self.path}: expected a list, got {type(raw).__name__}."
            )
        return [self._parse(entry, index) for index, entry in enumerate(raw)]

    def _write(self, candidates: list[Candidate]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps([asdict(c) for c in candidates], indent=2) + "\n"
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.path)

    @staticmethod
    def _parse(entry: Any, index: int) -> Candidate:
        if not isinstance(entry, dict):
            raise CandidateStoreError(
                f"candidates[{index}]: expected an object, got {type(entry).__name__}."
            )
        try:
            return Candidate(
                id=entry["id"],
                kind=entry["kind"],
                target_id=entry["target_id"],
                fields=entry["fields"],
                source=entry.get("source", ""),
                marks=tuple(entry.get("marks", ())),
                disagrees_with_survey=entry.get("disagrees_with_survey", False),
                name_is_a_stand_in=entry.get("name_is_a_stand_in", False),
                found_at=entry["found_at"],
            )
        except KeyError as error:
            raise CandidateStoreError(
                f"candidates[{index}]: missing required field {error.args[0]!r}."
            ) from error

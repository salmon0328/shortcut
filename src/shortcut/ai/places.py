"""Turning a phrase like "the courtyard" into a node id. No model involved.

This is deliberately ordinary code sitting between the two AI steps, and the
split matters: the model reads a sentence and hands back the *phrases* it
found, and this module decides which place each phrase means. A model that
emits node ids invents node ids. A model that emits phrases cannot.

The scoring tiers are a port of ``matchScore`` in ``web/src/searchBox.js``, so
the suggestions the backend resolves to and the ones the autocomplete offers
cannot drift apart. Two things are added on top:

* **aliases**, because nobody says "Pick Lockers", they say "the lockers"
* **typo tolerance**, via :mod:`difflib` from the standard library

Ambiguity is reported, never guessed at. "Staircase 1" names two different
places in this building - one on B5, one on B4 - so any resolver that silently
picks one is wrong roughly half the time. It asks instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from shortcut.graph_store import CampusGraph, Node

__all__ = [
    "AMBIGUITY_MARGIN",
    "CONFIDENCE_FLOOR",
    "PLACE_ALIASES",
    "PlaceMatch",
    "resolve_place",
    "suggest_places",
]


# How close the runner-up has to be before we stop trusting the winner. Two
# places named the same thing score identically, so the gap is zero and the
# question gets asked.
AMBIGUITY_MARGIN = 0.08

# Below this, the best match is not good enough to act on even unopposed.
CONFIDENCE_FLOOR = 0.45

# How alike two strings must be before we treat the difference as a typo.
_FUZZY_FLOOR = 0.72

MAX_SUGGESTIONS = 8


# What students actually say, mapped to what the survey called it. Hand-written
# and building-specific: these are guesses about the Hive that the survey team
# should confirm on site, not facts derived from data.
PLACE_ALIASES: dict[str, str] = {
    "courtyard": "Hive_B5_G",
    "the courtyard": "Hive_B5_G",
    "atrium": "Hive_B5_G",
    "lockers": "Hive_B5_F",
    "the lockers": "Hive_B5_F",
    "parcel lockers": "Hive_B5_F",
    "pickup lockers": "Hive_B5_F",
    "entrance": "Hive_B5_I",
    "the entrance": "Hive_B5_I",
    "main door": "Hive_B5_I",
    "front door": "Hive_B5_I",
    "main entrance": "Hive_B5_I",
    "the lift": "Hive_B5_A",
    "elevator": "Hive_B5_A",
    "elevator lobby": "Hive_B5_A",
}

# Deliberately absent: "lift", "lift lobby", "main staircase". Each of those
# names a real place on B5 *and* on B4, so they are meant to come back
# ambiguous. An alias would quietly pick the B5 one - exactly the guessing
# this module exists to avoid.


@dataclass(frozen=True)
class PlaceMatch:
    """One candidate place, and why it was offered."""

    node_id: str
    name: str
    building: str
    floor: str
    score: float
    why: str

    @property
    def label(self) -> str:
        """How to name this place back to a person, unambiguously."""
        return f"{self.name} ({self.building} · {self.floor})"


def _clean(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _score(node: Node, query: str) -> tuple[float, str]:
    """Score one node against a cleaned query. Returns (score, why)."""
    name = node.name.lower()
    everything = f"{node.name} {node.building} {node.floor} {node.id}".lower()

    # An exact id is the most certain signal there is - it is what the picker
    # sends and what scripts use - so it ranks with an exact name, not down in
    # the substring tier where it would fall below the confidence floor.
    if node.id.lower() == query:
        return 1.0, "that is the place's id"
    if name == query:
        return 1.0, "name matches exactly"
    if name.startswith(query):
        return 0.80, "name starts with what you typed"
    if query in name:
        return 0.60, "name contains what you typed"

    # Every word matched, in any order, across the name, building and floor.
    # The picker labels places "Lift Lobby (Hive · B4)", so somebody typing
    # "Hive B4 Lift Lobby" is repeating what the app itself taught them - and
    # every ordered check above misses it, because no single field holds that
    # whole string. It is also the only way to name one of two places that
    # share a name without knowing its id.
    #
    # Multi-word queries only: for a single word the tiers above have already
    # said everything there is to say, and this would just promote weak hits.
    words = query.split()
    if len(words) > 1 and all(word in everything for word in words):
        return 0.55, "matches the place, building and floor you named"

    if query in everything:
        return 0.40, "matches the building, floor or id"

    # Last resort: allow for a typo, but only against the name. Scaled below
    # every exact tier so a real match always outranks a near-miss.
    ratio = SequenceMatcher(None, query, name).ratio()
    if ratio >= _FUZZY_FLOOR:
        return round(ratio * 0.5, 4), "close to a place name"
    return -1.0, ""


def _alias_for(query: str) -> str | None:
    """Look a phrase up in the alias table, allowing for a leading article.

    The full phrase is tried first, and that ordering carries weight: "the
    lift" has an entry of its own naming the B5 lift, while bare "lift" names
    one on every floor and is meant to be ambiguous. Only when nothing matches
    is "the" dropped, so "the main entrance" finds "main entrance" without
    "the lift" ever collapsing into "lift".
    """
    exact = PLACE_ALIASES.get(query)
    if exact is not None:
        return exact
    without_article = re.sub(r"^(?:the|a|an)\s+", "", query)
    return PLACE_ALIASES.get(without_article) if without_article != query else None


def suggest_places(
    graph: CampusGraph, phrase: str, *, limit: int = MAX_SUGGESTIONS
) -> list[PlaceMatch]:
    """Every place ``phrase`` could mean, best first.

    An empty phrase returns nothing rather than everything: the caller asked
    about something, and "all of them" is not an answer to that.
    """
    query = _clean(phrase)
    if not query:
        return []

    aliased = _alias_for(query)
    matches: list[PlaceMatch] = []

    for node in graph.nodes.values():
        if node.id == aliased:
            score, why = 0.95, "a common name for this place"
        else:
            score, why = _score(node, query)
        if score < 0:
            continue
        matches.append(
            PlaceMatch(
                node_id=node.id,
                name=node.name,
                building=node.building,
                floor=node.floor,
                score=score,
                why=why,
            )
        )

    # Sort by score, then by node id so equal scores order the same way every
    # run. A resolver that shuffles its own ties is untestable.
    matches.sort(key=lambda match: (-match.score, match.node_id))
    return matches[:limit]


@dataclass(frozen=True)
class PlaceResolution:
    """What we concluded about one phrase."""

    phrase: str
    resolved: PlaceMatch | None
    alternatives: list[PlaceMatch]
    ambiguous: bool
    reason: str

    @property
    def question(self) -> str | None:
        """The clarifying question to ask, if one is needed."""
        if not self.ambiguous or len(self.alternatives) < 2:
            return None
        first, second = self.alternatives[0], self.alternatives[1]
        return f"Did you mean {first.label} or {second.label}?"


def resolve_place(graph: CampusGraph, phrase: str) -> PlaceResolution:
    """Decide which place ``phrase`` means, or report that it is unclear.

    Three outcomes, and the third is the one that earns its keep:

    * one clear winner - resolved
    * nothing close enough - unresolved, and we say so
    * two places the phrase fits equally well - unresolved, with both offered
    """
    matches = suggest_places(graph, phrase)

    if not matches:
        return PlaceResolution(
            phrase=phrase,
            resolved=None,
            alternatives=[],
            ambiguous=False,
            reason="no place here matches that",
        )

    best = matches[0]
    runner_up = matches[1] if len(matches) > 1 else None

    if best.score < CONFIDENCE_FLOOR:
        return PlaceResolution(
            phrase=phrase,
            resolved=None,
            alternatives=matches[:2],
            ambiguous=True,
            reason="nothing matches that closely enough to be sure",
        )

    if runner_up is not None and (best.score - runner_up.score) < AMBIGUITY_MARGIN:
        return PlaceResolution(
            phrase=phrase,
            resolved=None,
            alternatives=[best, runner_up],
            ambiguous=True,
            reason="more than one place goes by that name",
        )

    return PlaceResolution(
        phrase=phrase,
        resolved=best,
        alternatives=matches[1:3],
        ambiguous=False,
        reason=best.why,
    )

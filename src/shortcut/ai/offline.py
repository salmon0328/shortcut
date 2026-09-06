"""Reading a request with rules instead of a model.

This exists so the plain-language box works on a laptop with no AWS account,
no credentials and no network. Three reasons that matters more than it looks:

* a teammate who clones this repo can see the feature work in the first
  minute, rather than after an afternoon of IAM
* the demo survives the venue wifi, which is not a hypothetical
* it is a floor under the whole feature - when the model is unreachable the
  box degrades to something slightly dumber, not to an error message

It is emphatically not an agent, and not clever. It looks for "X to Y", a few
words about rain and stairs, and a number followed by "minutes". Anything it
cannot find it leaves as ``None``, which is exactly what
:class:`~shortcut.ai.schemas.ParsedIntent` means by "the person did not say" -
so a request it half-understands still behaves correctly rather than guessing.

The real model does this better, and does it for sentences nobody anticipated.
That is the difference worth showing a judge: same seam, same schema, and the
quality of the reading is the only thing that changes.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from shortcut.ai.bedrock import Image, LlmError
from shortcut.ai.schemas import ParsedIntent
from shortcut.ai.usage import TokenUsage

__all__ = ["KeywordLlm"]


# "from A to B" is stated outright, so it is tried first. Then the shape
# people actually type, which buries the origin behind "I'm at". Only then
# the bare "A to B", which is right far more often than it is wrong.
_ROUTE_PATTERNS = (
    re.compile(r"\bfrom\s+(?P<origin>.+?)\s+to\s+(?P<destination>.+)", re.I),
    # The same thing said backwards, which is just as common out loud:
    # "how do I get to the courtyard from the main entrance".
    re.compile(r"\bto\s+(?P<destination>.+?)\s+from\s+(?P<origin>.+)", re.I),
    re.compile(
        r"\b(?:i'?m|i am)\s+(?:at|in|near|by)\s+(?P<origin>.+?)"
        r"[,;]?\s+(?:and\s+)?(?:i\s+)?(?:need|want|have|got|gotta)"
        # "need to get to the lab", "need to be at the lab", "need the lab" -
        # the middle of that is all optional, and so is the final "to".
        r"(?:\s+to\s+(?:get|go|be|head))?\s*(?:\s+(?:to|at|in))?"
        r"\s+(?P<destination>.+)",
        re.I,
    ),
    re.compile(r"^(?P<origin>.+?)\s+to\s+(?P<destination>.+)$", re.I),
)

# Openers that carry no meaning, stripped before anything else is tried.
_FILLER = re.compile(
    r"^\s*(?:hi|hey|please|can you|could you|how do i|how can i|"
    r"i need|i want|take me|get me|show me|bring me|navigate me|"
    r"what'?s the way|which way)\b[\s,]*",
    re.I,
)

_RAIN = re.compile(r"\b(?:rain|raining|wet|drizzl|pour|downpour|dry|shelter)", re.I)
# A *hard* shelter requirement, as opposed to _RAIN, which only says the
# weather came up. The difference decides whether shelter becomes a filter
# that rules routes out or a preference that merely prices them, so the
# wordings people actually use for the strong version all have to land here:
# "must be sheltered" is a requirement in anyone's reading of it, and matching
# only "must stay dry" left it as a mere preference.
_MUST_STAY_DRY = re.compile(
    r"\b(?:can'?t|cannot|must not|mustn'?t)\s+get\s+wet"
    r"|\bmust\s+(?:stay|keep)\s+dry\b"
    # "must be sheltered", "needs to be covered", "has to be indoors"
    r"|\b(?:must|need(?:s)?\s+to|has\s+to|have\s+to|got\s+to)\s+be\s+"
    r"(?:sheltered|covered|indoors?|dry|under\s+cover)\b"
    # "sheltered only", "covered route only"
    r"|\b(?:sheltered|covered)\s+(?:\w+\s+)?only\b"
    r"|\bstay\s+(?:indoors?|inside|under\s+cover|dry)\b"
    r"|\bkeep\s+me\s+dry\b",
    re.I,
)
_NO_STAIRS = re.compile(
    r"\b(?:no|avoid|without|can'?t|cannot|can not)\s+(?:use\s+|do\s+|take\s+)?"
    r"(?:the\s+)?stairs?\b|\bstep[- ]free\b|\bwheelchair\b",
    re.I,
)
_HARD_ON_STAIRS = re.compile(
    r"\b(?:knee|ankle|crutch|injur|sprain|luggage|suitcase|heavy|"
    r"trolley|pram|stroller)", re.I
)
_RIDE = re.compile(r"\b(?:shuttle|bus)\b", re.I)
_HURRY = re.compile(r"\b(?:fastest|quickest|quick|hurry|rush|late|asap)\b", re.I)
_MINUTES = re.compile(r"\b(\d{1,3})\s*(?:min|mins|minutes)\b", re.I)

# Everything after one of these is a condition, not part of a place name:
# "the courtyard, it's raining" names one place, not two.
_TAIL = re.compile(r"[,;.]| but | and (?:it|i|there)\b| because | since ", re.I)


# A deadline tacked onto the end of a place: "the courtyard in 5 minutes".
# The number is still read as a time budget from the full sentence; this only
# stops it being mistaken for part of the name.
_TRAILING_TIME = re.compile(
    r"\s+(?:in|within|by|before)\s+\d+\s*(?:min|mins|minutes|hrs?|hours?)\b.*$",
    re.I,
)


def _clean_phrase(raw: str) -> str:
    """Trim a captured phrase down to just the place."""
    phrase = _TAIL.split(raw, maxsplit=1)[0]
    phrase = _TRAILING_TIME.sub("", phrase)
    # Strip a dangling "to" left behind by "take me to ...", and nothing more.
    # "the" in particular is left alone: "the lift" is an alias for one
    # specific lift, while bare "lift" names one on every floor, so trimming
    # the article would turn an answerable request into a question.
    phrase = re.sub(r"^\s*(?:to|towards|at|in)\s+", "", phrase, flags=re.I)
    return phrase.strip(" \t\"'?!.")


def _split_places(text: str) -> tuple[str | None, str]:
    """Find where they are and where they are going, as they said it."""
    stripped = _FILLER.sub("", text).strip()
    for pattern in _ROUTE_PATTERNS:
        match = pattern.search(stripped)
        if not match:
            continue
        origin = _clean_phrase(match.group("origin"))
        destination = _clean_phrase(match.group("destination"))
        if destination:
            return (origin or None), destination

    # No "to" anywhere: treat the whole thing as a destination. Someone typing
    # "the courtyard" into a box labelled "where do you need to go" means it.
    return None, _clean_phrase(stripped) or stripped.strip()


def read_intent(text: str) -> ParsedIntent:
    """Turn one sentence into an intent, by rule. Exposed for testing."""
    origin, destination = _split_places(text)

    avoid_stairs = True if _NO_STAIRS.search(text) else None
    wants_shelter = True if _MUST_STAY_DRY.search(text) else None

    preference: str | None = None
    if avoid_stairs or _HARD_ON_STAIRS.search(text):
        preference = "prefer_lift"
    elif _RIDE.search(text):
        preference = "least_walking"
    elif _RAIN.search(text):
        preference = "sheltered"
    elif _HURRY.search(text):
        preference = "fastest"

    minutes = _MINUTES.search(text)

    return ParsedIntent(
        origin_phrase=origin,
        destination_phrase=destination,
        preference=preference,
        avoid_stairs=avoid_stairs,
        wants_shelter=wants_shelter,
        max_minutes=float(minutes.group(1)) if minutes else None,
        notes="",
    )


class KeywordLlm:
    """An :class:`~shortcut.ai.bedrock.LlmPort` backed by regular expressions.

    Answers the parse call and nothing else. Anything needing real reading -
    the photo work especially - raises rather than returning a confident
    guess, because a wrong fact about a corridor is worse than no fact.
    """

    @property
    def model_id(self) -> str:
        return "offline-keywords"

    def structured(
        self, schema: type[BaseModel], *, purpose: str, system: str, user: str
    ) -> tuple[BaseModel, TokenUsage]:
        if schema is not ParsedIntent:
            raise LlmError(
                f"{purpose}: the offline reader only handles route requests, "
                f"not {schema.__name__}. Set MOCK_MODE=false and configure "
                f"Bedrock to use this."
            )
        # The user turn is "Student request:\n<text>"; take what follows.
        text = user.split("\n", 1)[-1]
        return read_intent(text), TokenUsage(calls=1, model_id=self.model_id)

    def vision(
        self,
        schema: type[BaseModel],
        *,
        purpose: str,
        system: str,
        user: str,
        images: list[Image],
    ) -> tuple[BaseModel, TokenUsage]:
        raise LlmError(
            f"{purpose}: reading photographs needs a real model. Set "
            f"MOCK_MODE=false and configure Bedrock."
        )

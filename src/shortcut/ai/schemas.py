"""Every shape the AI layer produces or accepts.

Two rules hold across this whole file:

* ``extra="forbid"`` everywhere. A model that invents a field should fail
  validation loudly rather than have the field quietly dropped.
* Choices are ``Literal``, never free strings. The Ranking Agent picks a
  candidate by index because a ``Literal["c0", "c1", "c2"]`` cannot express a
  route that was never offered. The guarantee is in the type, not the prompt.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from shortcut.ai.usage import TokenUsage
from shortcut.schemas import RoutePreference, RouteRequest

__all__ = [
    "AiHealth",
    "ParseRequestBody",
    "ParseResult",
    "ParsedIntent",
    "PlaceCandidate",
    "PlaceChoice",
    "RoutePreferences",
]


class Strict(BaseModel):
    """Base for everything here: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# What the model returns when it reads a sentence
# --------------------------------------------------------------------------


class ParsedIntent(Strict):
    """The model's reading of one request. It resolves nothing.

    Every optional field means "the person did not say", which is different
    from "the person said no". That distinction is why they are ``None`` and
    not ``False``: a request that never mentions stairs must leave the user's
    existing toggle alone rather than silently switching it off.

    Note there are no node ids here. The model returns the *words* it found,
    and :mod:`shortcut.ai.places` decides what they refer to. A model that
    emits ids will eventually emit an id that does not exist.
    """

    origin_phrase: str | None = Field(
        default=None,
        max_length=120,
        description="Where the person said they are starting, in their own words. "
        "Null if they did not say.",
    )
    destination_phrase: str = Field(
        max_length=120,
        description="Where the person said they are going, in their own words.",
    )
    preference: RoutePreference | None = Field(
        default=None,
        description="How they want the route scored, if they said. 'sheltered' when "
        "they mention rain or staying dry; 'prefer_lift' when they mention avoiding "
        "stairs or carrying something; 'least_walking' when they would rather ride.",
    )
    avoid_stairs: bool | None = Field(
        default=None, description="True only if they said they cannot or will not use stairs."
    )
    avoid_lift: bool | None = Field(default=None, description="True only if they refused lifts.")
    avoid_shuttle: bool | None = Field(
        default=None, description="True only if they refused the shuttle."
    )
    wants_shelter: bool | None = Field(
        default=None,
        description="True if staying dry is a hard requirement ('I must not get wet'), "
        "rather than a preference ('it's raining').",
    )
    max_minutes: float | None = Field(
        default=None,
        gt=0,
        le=180,
        description="A time budget in minutes, if they gave one ('I have ten minutes').",
    )
    notes: str = Field(
        default="",
        max_length=200,
        description="Anything relevant that no other field captures. Usually empty.",
    )


# --------------------------------------------------------------------------
# What we resolve those words to
# --------------------------------------------------------------------------


class PlaceCandidate(Strict):
    """One place a phrase might mean."""

    node_id: str
    name: str
    building: str
    floor: str
    score: float
    why: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.building} · {self.floor})"


class PlaceChoice(Strict):
    """What we concluded about one phrase, and what to ask if unsure."""

    phrase: str
    resolved: PlaceCandidate | None = None
    alternatives: list[PlaceCandidate] = Field(default_factory=list)
    ambiguous: bool = False
    reason: str = ""


class RoutePreferences(Strict):
    """How the route should be scored, once the sentence and the controls agree.

    Everything a :class:`~shortcut.schemas.RouteRequest` carries except the two
    places, so it can be reported even when there is no request to build.

    That case is the reason this exists. "Take me to the lift lobby, I can't
    use stairs" is ambiguous about the lift lobby, so no request is built - and
    without this the refusal of stairs would be read, dropped on the floor, and
    the route the student finally picks would send them up a staircase they
    have just said they cannot climb.
    """

    preference: RoutePreference = "fastest"
    allow_stairs: bool = True
    allow_lift: bool = True
    allow_shuttle: bool = True
    sheltered_only: bool = False


class ParseResult(Strict):
    """A sentence, turned into something the deterministic router can run.

    ``request`` is a real :class:`~shortcut.schemas.RouteRequest`, built only
    when both ends resolved. That is deliberate: it means the plain-language
    path hands the existing endpoint exactly what the dropdown path does, and
    every validation rule already written applies unchanged.

    ``preferences`` is always present, and carries the same choices for the
    times when ``request`` is ``None``.
    """

    request: RouteRequest | None = None
    preferences: RoutePreferences = Field(default_factory=RoutePreferences)
    origin: PlaceChoice
    destination: PlaceChoice
    needs_clarification: bool = False
    question: str | None = None
    # Kept beside the request rather than inside it: a time budget is something
    # the agent judges against, not something A* can price.
    max_minutes: float | None = None
    notes: str = ""
    usage: TokenUsage = Field(default_factory=TokenUsage.empty)


class ParseRequestBody(Strict):
    """One sentence typed into the box, plus whatever the controls say.

    ``current`` is not optional decoration. Every three-valued field in
    :class:`ParsedIntent` means "the person did not mention this", and that
    only means something if there is an existing setting to leave alone.
    Without it, a request that says nothing about stairs would silently reset
    a toggle the user had deliberately switched.
    """

    # Trim before validating, so a stray newline from a textarea does not
    # count towards the length limit or read as content.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(
        min_length=1,
        max_length=500,
        description="What the student typed, in their own words.",
    )
    current: RouteRequest | None = Field(
        default=None,
        description="What the route controls already show, if anything.",
    )


# --------------------------------------------------------------------------
# Service health
# --------------------------------------------------------------------------


class AiHealth(Strict):
    """What ``GET /ai/health`` reports, so a demo failure is diagnosable."""

    available: bool
    mock_mode: bool
    model_id: str
    region: str
    detail: str

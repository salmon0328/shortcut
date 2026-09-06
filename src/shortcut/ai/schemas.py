"""Every shape the AI layer produces or accepts.

Two rules hold across this whole file:

* ``extra="forbid"`` everywhere. A model that invents a field should fail
  validation loudly rather than have the field quietly dropped.
* Choices are ``Literal``, never free strings. The Ranking Agent picks a
  candidate by index because a ``Literal["c0", "c1", "c2"]`` cannot express a
  route that was never offered. The guarantee is in the type, not the prompt.
"""

from __future__ import annotations

from typing import Literal

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
    "ReportImpact",
    "ReportReading",
    "ReportVerdict",
    "ReportWeight",
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
        # A place with no floor is outdoors - the walkway between the
        # buildings, a road-level entrance - so the floor is left out rather
        # than printed as an empty half of a separator.
        where = " · ".join(part for part in (self.building, self.floor) if part)
        return f"{self.name} ({where})"


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
# Judging reported problems
# --------------------------------------------------------------------------


class ReportWeight(Strict):
    """How much one submission should count towards believing a problem.

    A weight, not a verdict. The model reads what somebody typed and says how
    much it is worth; the *decision* is arithmetic done afterwards against a
    threshold in :mod:`shortcut.ai.config`. Keeping those apart is the same
    split as the parser's - the model reads, the code decides - and it is what
    stops a persuasive sentence from talking its way past a limit.
    """

    report_id: str = Field(description="Which submission this is about.")
    weight: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "1.0 for a first-hand, specific, checkable account ('lift 2 has an "
            "out-of-order sign, engineer on site'). Around 0.5 for a plausible "
            "but bare one ('lift broken'). 0.0 for anything vague, empty, "
            "joking, or describing something other than the reported problem."
        ),
    )
    why: str = Field(
        max_length=200, description="One short sentence of justification."
    )


class ReportReading(Strict):
    """What the model made of every submission about one problem."""

    weights: list[ReportWeight] = Field(
        description="One entry per submission, in the order they were given."
    )
    describes_condition: bool = Field(
        description=(
            "Whether the notes actually describe the condition claimed. False "
            "when people report a lift as 'blocked' but describe it as merely "
            "busy - the problem may be real and still be filed as the wrong "
            "kind."
        )
    )
    summary: str = Field(
        max_length=300,
        description="What these submissions collectively claim, in one or two lines.",
    )
    contradiction: str = Field(
        default="",
        max_length=300,
        description=(
            "Where the submissions disagree with each other, if they do. Empty "
            "when they agree."
        ),
    )


class ReportImpact(Strict):
    """What approving the report would do to routing, worked out by arithmetic."""

    blocks_routes: bool
    cut_off: list[str] = Field(
        default_factory=list,
        description="Places that would lose every way in. Empty is the normal case.",
    )
    detour_seconds: float | None = Field(
        default=None, description="How much longer the way round would be."
    )
    describes: str = Field(default="", description="The same, as one readable line.")


class ReportVerdict(Strict):
    """What the Verifier decided, and everything it decided from.

    Every number that went into the decision is here, because an agent that
    changes the map has to be answerable for it afterwards. A reviewer looking
    at this should be able to disagree with the outcome and see exactly which
    step they disagree with.
    """

    key: str
    action: Literal["approved", "rejected", "escalated"] = Field(
        description=(
            "'escalated' means the agent declined to decide and left it in the "
            "queue for a person - the outcome for anything well-corroborated "
            "but consequential, which is most of what matters."
        )
    )
    weight: float = Field(description="The submissions' weights, added up.")
    threshold: float = Field(description="What the weight had to beat, after any raise.")
    base_threshold: float = Field(description="What it would have been, before.")
    raised_because: str = Field(
        default="",
        description="Why the bar went up, when it did. Empty when it did not.",
    )
    impact: ReportImpact
    reading: ReportReading
    why: str = Field(description="The decision in one sentence, for a person to read.")
    applied: bool = Field(
        default=False,
        description="Whether the map actually changed. False for anything not approved.",
    )
    usage: TokenUsage = Field(default_factory=TokenUsage.empty)


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

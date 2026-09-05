"""Settings for the agentic layer: which model, which region, which limits.

Everything the AI code needs to know about the outside world is read once,
here, and handed around as a frozen :class:`AiSettings`. Two rules this module
exists to enforce:

* **Model ids are constants, never string-built.** At least one Bedrock id in
  the wild breaks the usual ``-YYYYMMDD-vN:0`` convention, so an f-string that
  assembles ids works for months and then fails on one model. Copy the id from
  the console and paste it below.
* **Every loop limit lives here, in code.** ``MAX_REPLANS`` is read by the
  graph's router edge, not by a prompt. A model cannot talk its way past a
  number it never sees.

Nothing here does network I/O on import.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from shortcut.dotenv import load_dotenv

__all__ = [
    "AUTO_MERGE_THRESHOLD_ADVISORY",
    "AUTO_MERGE_THRESHOLD_BLOCKING",
    "AiSettings",
    "BEDROCK_MODEL_ID",
    "BEDROCK_MODEL_ID_APAC",
    "MAX_GATE_RETRIES",
    "MAX_REPLANS",
    "MAX_TOOL_CALLS",
    "load_settings",
]


# --------------------------------------------------------------------------
# Model ids. Copied from the Bedrock console. Never assembled.
# --------------------------------------------------------------------------

# The global inference profile, which is what the workshop lab defaults to.
BEDROCK_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

# The APAC cross-region profile. Try this one if the global profile does not
# resolve in ap-southeast-1; `scripts/check_bedrock.py` reports which works.
BEDROCK_MODEL_ID_APAC = "apac.anthropic.claude-haiku-4-5-20251001-v1:0"

DEFAULT_REGION = "ap-southeast-1"


# --------------------------------------------------------------------------
# Hard limits. These are the guardrails, and they are deliberately dull
# integers rather than anything a model can negotiate with.
# --------------------------------------------------------------------------

#: How many times the Ranking Agent may change its own request and search
#: again. Held in graph state and checked by a router edge.
MAX_REPLANS = 3

#: How many times a single verifier gate may be retried on a malformed answer
#: before the report is escalated to a human.
MAX_GATE_RETRIES = 2

#: How many tool calls the Verifier's plausibility gate may make in one run.
MAX_TOOL_CALLS = 3

#: Weighted corroboration needed before a report changes the map on its own.
#: Higher for conditions that close a route than for advisory ones, because
#: wrongly closing a corridor is worse than wrongly labelling it busy.
AUTO_MERGE_THRESHOLD_BLOCKING = 2.0
AUTO_MERGE_THRESHOLD_ADVISORY = 1.0


def _truthy(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AiSettings:
    """Everything the AI layer reads from the environment, resolved once."""

    model_id: str
    region: str
    profile: str | None
    mock_mode: bool

    @property
    def describes(self) -> str:
        """A one-line label for logs and ``GET /ai/health``."""
        return f"{'mock' if self.mock_mode else 'bedrock'}:{self.model_id}"


def load_settings(project_root: Path | None = None) -> AiSettings:
    """Resolve settings from ``.env`` and the environment.

    ``MOCK_MODE`` defaults to **true**, which is the important default: a
    fresh clone runs, its tests pass and the demo path replays without any
    AWS credentials at all. Turning the real model on is an explicit act.
    """
    root = project_root or Path(__file__).resolve().parents[3]
    # The server has normally read this already at startup; doing it again is
    # harmless (only unset names are filled) and keeps the AI settings correct
    # when this package is used on its own, as in scripts/check_bedrock.py.
    load_dotenv(root / ".env")
    return AiSettings(
        model_id=os.environ.get("MODEL_ID") or BEDROCK_MODEL_ID,
        region=os.environ.get("AWS_REGION") or DEFAULT_REGION,
        profile=os.environ.get("AWS_PROFILE") or None,
        mock_mode=_truthy(os.environ.get("MOCK_MODE"), default=True),
    )

"""The one place that talks to a model, and the fake that stands in for it.

Every AI component in this package receives an :class:`LlmPort` rather than
importing a client. That single decision is what makes the other four files
testable: the whole agentic layer runs offline, deterministically, against
:class:`MockLlm`, and switching to the real thing changes no component code.

Two methods, because there are only two shapes of call in this project:

* :meth:`LlmPort.structured` - text in, a validated Pydantic model out.
* :meth:`LlmPort.vision` - the same, with images attached.

Both take a ``purpose``. It names the graph node making the call, which is
what the log line reports and what :class:`MockLlm` keys its canned answers
on. Passing it is not optional, because an unlabelled model call is one you
cannot cost, cache or fake.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from shortcut.ai.config import AiSettings
from shortcut.ai.usage import TokenUsage

__all__ = ["BedrockLlm", "Image", "LlmError", "LlmPort", "MockLlm", "build_llm"]

logger = logging.getLogger("shortcut.ai")

T = TypeVar("T", bound=BaseModel)

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class LlmError(RuntimeError):
    """A model call failed, or came back as something we cannot use."""


@dataclass(frozen=True)
class Image:
    """One image, ready to attach to a request."""

    data: bytes
    mime_type: str
    label: str = ""

    @classmethod
    def from_path(cls, path: Path, label: str = "") -> "Image":
        suffix = path.suffix.lower()
        mime = _MIME_BY_SUFFIX.get(suffix)
        if mime is None:
            raise LlmError(
                f"{path.name}: cannot send a {suffix or 'suffixless'} file to the model. "
                f"Supported: {', '.join(sorted(_MIME_BY_SUFFIX))}."
            )
        return cls(data=path.read_bytes(), mime_type=mime, label=label or path.name)

    def as_content_block(self) -> dict[str, Any]:
        """The LangChain standard content block for an inline image."""
        return {
            "type": "image",
            "source_type": "base64",
            "data": base64.b64encode(self.data).decode("ascii"),
            "mime_type": self.mime_type,
        }


class LlmPort(Protocol):
    """What the agentic code is allowed to ask of a model."""

    def structured(
        self, schema: type[T], *, purpose: str, system: str, user: str
    ) -> tuple[T, TokenUsage]:
        """Return an instance of ``schema``, plus what it cost."""

    def vision(
        self,
        schema: type[T],
        *,
        purpose: str,
        system: str,
        user: str,
        images: list[Image],
    ) -> tuple[T, TokenUsage]:
        """The same, with ``images`` attached to the user turn."""


# --------------------------------------------------------------------------
# The real one
# --------------------------------------------------------------------------


class BedrockLlm:
    """Claude on Amazon Bedrock, through LangChain's Converse wrapper.

    Structured output is requested with ``include_raw=True`` so the untouched
    response comes back alongside the parsed model. That is the only way to
    read token usage, which we need on every call.
    """

    def __init__(self, settings: AiSettings) -> None:
        # Imported here, not at module scope: a machine with no AI extras
        # installed can still import this module and use MockLlm.
        from langchain_aws import ChatBedrockConverse  # noqa: PLC0415

        self._settings = settings
        client_kwargs: dict[str, Any] = {
            "model": settings.model_id,
            "region_name": settings.region,
            # Deterministic-ish: this is a classifier and a chooser, not a
            # writer. We want the same answer for the same route twice.
            "temperature": 0.0,
            "max_tokens": 1024,
        }
        if settings.profile:
            client_kwargs["credentials_profile_name"] = settings.profile
        self._chat = ChatBedrockConverse(**client_kwargs)

    @property
    def model_id(self) -> str:
        return self._settings.model_id

    def structured(
        self, schema: type[T], *, purpose: str, system: str, user: str
    ) -> tuple[T, TokenUsage]:
        return self._invoke(schema, purpose=purpose, system=system, content=user)

    def vision(
        self,
        schema: type[T],
        *,
        purpose: str,
        system: str,
        user: str,
        images: list[Image],
    ) -> tuple[T, TokenUsage]:
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        content.extend(image.as_content_block() for image in images)
        return self._invoke(schema, purpose=purpose, system=system, content=content)

    def _invoke(
        self,
        schema: type[T],
        *,
        purpose: str,
        system: str,
        content: str | list[dict[str, Any]],
    ) -> tuple[T, TokenUsage]:
        from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

        messages = [SystemMessage(content=system), HumanMessage(content=content)]
        started = time.monotonic()
        try:
            result = self._chat.with_structured_output(schema, include_raw=True).invoke(
                messages
            )
        except Exception as error:  # noqa: BLE001 - re-raised as our own type
            raise LlmError(f"{purpose}: model call failed: {error}") from error

        parsed = result.get("parsed")
        if parsed is None:
            detail = result.get("parsing_error") or "no parsed output returned"
            raise LlmError(f"{purpose}: model did not return valid {schema.__name__}: {detail}")

        usage = self._usage_from(result.get("raw"))
        logger.info(
            "llm purpose=%s model=%s input=%d output=%d ms=%d",
            purpose,
            self.model_id,
            usage.input_tokens,
            usage.output_tokens,
            round((time.monotonic() - started) * 1000),
        )
        return parsed, usage

    def _usage_from(self, raw: Any) -> TokenUsage:
        """Pull token counts off the untouched response, tolerating absence."""
        metadata = getattr(raw, "usage_metadata", None) or {}
        return TokenUsage(
            input_tokens=int(metadata.get("input_tokens", 0) or 0),
            output_tokens=int(metadata.get("output_tokens", 0) or 0),
            calls=1,
            model_id=self.model_id,
        )


# --------------------------------------------------------------------------
# The fake
# --------------------------------------------------------------------------


class MockLlm:
    """A stand-in that answers from a dictionary instead of a model.

    Used in three places, all of which matter:

    * every test in this package, so the suite stays offline and deterministic
    * the frontend engineer's machine, before Bedrock access lands
    * the demo, when the venue wifi does what venue wifi does

    Answers are keyed by ``purpose``. Ask for a purpose it has not been given
    and it raises, loudly, rather than inventing something - a silent default
    would make a broken wiring look like a working one.
    """

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self._answers: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, str]] = []  # (purpose, schema name)
        for purpose, value in (answers or {}).items():
            self.register(purpose, value)

    @property
    def model_id(self) -> str:
        return "mock"

    def register(self, purpose: str, *values: Any) -> "MockLlm":
        """Queue one or more answers for ``purpose``.

        Several values mean several calls: the replan loop asks the same
        purpose more than once, and a test needs to say what happens each
        time. The last value is reused once the queue runs dry.
        """
        self._answers.setdefault(purpose, []).extend(values)
        return self

    def structured(
        self, schema: type[T], *, purpose: str, system: str, user: str
    ) -> tuple[T, TokenUsage]:
        return self._answer(schema, purpose)

    def vision(
        self,
        schema: type[T],
        *,
        purpose: str,
        system: str,
        user: str,
        images: list[Image],
    ) -> tuple[T, TokenUsage]:
        return self._answer(schema, purpose)

    def _answer(self, schema: type[T], purpose: str) -> tuple[T, TokenUsage]:
        self.calls.append((purpose, schema.__name__))
        queue = self._answers.get(purpose)
        if not queue:
            raise LlmError(
                f"MockLlm has no answer registered for purpose {purpose!r}. "
                f"Registered: {sorted(self._answers) or 'nothing'}."
            )
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(value, schema):
            parsed = value
        elif isinstance(value, dict):
            parsed = schema.model_validate(value)
        else:
            raise LlmError(
                f"MockLlm answer for {purpose!r} is a {type(value).__name__}; "
                f"expected {schema.__name__} or a dict."
            )
        # Plausible-but-fake numbers, so cost plumbing is exercised offline.
        return parsed, TokenUsage(input_tokens=800, output_tokens=60, calls=1, model_id="mock")


def build_llm(settings: AiSettings) -> LlmPort:
    """The real client, or the offline reader, depending on ``MOCK_MODE``.

    Note this returns :class:`~shortcut.ai.offline.KeywordLlm` and not
    :class:`MockLlm`. A MockLlm with nothing registered raises on every call,
    which is right in a test - an unregistered purpose means the wiring is
    wrong and should say so - and useless as a default, because it would make
    a fresh clone answer every request with an error. Tests construct MockLlm
    themselves and hand it in.
    """
    if settings.mock_mode:
        from shortcut.ai.offline import KeywordLlm  # noqa: PLC0415 - avoids a cycle

        return KeywordLlm()
    return BedrockLlm(settings)

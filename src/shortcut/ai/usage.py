"""Counting what an answer cost.

Every model call returns tokens alongside its result, and every AI response
model in this package carries a :class:`TokenUsage`. That is not bookkeeping
for its own sake: "what does one route request cost?" is a question we have to
answer with a number, and the only honest way to get one is to add it up as we
go rather than estimate it afterwards.

Usage merges rather than overwrites, so a request that makes three calls
reports the total of the three.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["TokenUsage"]


class TokenUsage(BaseModel):
    """Tokens spent producing one answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    calls: int = Field(default=0, ge=0, description="How many model calls this covers.")
    model_id: str = Field(default="", description="Which model produced it.")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merged(self, other: "TokenUsage") -> "TokenUsage":
        """Add ``other`` to this usage.

        The model id of whichever side actually made a call wins, so merging a
        real call into an empty starting total keeps the real id.
        """
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            calls=self.calls + other.calls,
            model_id=other.model_id or self.model_id,
        )

    @classmethod
    def empty(cls) -> "TokenUsage":
        """A zero total to start accumulating from."""
        return cls()

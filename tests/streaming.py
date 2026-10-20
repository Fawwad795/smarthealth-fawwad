"""Helpers shared by the streaming tests (tasks 5.8 and 5.9): reading an
SSE body, and two stand-in models that misbehave part-way through a stream.

TestClient reads a whole stream before returning it, so read_events()
checks the events and their order, never their timing.
"""

import json
from collections.abc import AsyncIterator

import anyio
from httpx import Response

from app.ai.llm import (
    DEFAULT_MAX_TOKENS,
    FakeLLM,
    LLMChunk,
    LLMMessage,
    TransientLLMError,
)


def read_events(response: Response) -> list[tuple[str, dict]]:
    """The events of an SSE response, in order, as (name, data) pairs."""
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    events = []
    for block in response.text.removesuffix("\n\n").split("\n\n"):
        name, data = block.split("\n")
        events.append(
            (name.removeprefix("event: "), json.loads(data.removeprefix("data: ")))
        )
    return events


def streamed_text(events: list[tuple[str, dict]]) -> str:
    """Every token event's text, joined in order."""
    return "".join(data["text"] for name, data in events if name == "token")


class BreaksMidStream(FakeLLM):
    """Answers complete() from its replies, then streams `piece` and fails,
    as a dropped connection would."""

    def __init__(self, *replies: str, piece: str = "Knee ") -> None:
        super().__init__(*replies)
        self.piece = piece

    async def stream(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> AsyncIterator[LLMChunk]:
        """Record the call, yield `piece`, then fail."""
        self.calls.append(list(messages))
        yield LLMChunk(text=self.piece)
        raise TransientLLMError("LLM provider unreachable (ReadError)")


class StallsMidStream(FakeLLM):
    """Answers complete() from its replies, then streams `piece` and goes
    quiet, as a slow provider would. Notes when its stream is closed."""

    def __init__(self, *replies: str, piece: str = "Knee ") -> None:
        super().__init__(*replies)
        self.piece = piece
        self.closed = False

    async def stream(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> AsyncIterator[LLMChunk]:
        """Record the call, yield `piece`, then wait until closed."""
        self.calls.append(list(messages))
        try:
            yield LLMChunk(text=self.piece)
            await anyio.sleep_forever()
        finally:
            self.closed = True

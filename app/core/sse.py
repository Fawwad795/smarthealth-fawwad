"""Server-sent events: the response class and event format every streaming
endpoint uses (task 5.8).

One place for both, so /assistant/ask/stream and the staff generation
streams (task 5.9) send the same format and close their streams the same way.
"""

from collections.abc import AsyncGenerator

from pydantic import BaseModel
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class SSEResponse(StreamingResponse):
    """A text/event-stream response that closes its generator when it ends.

    Starlette 0.41 stops reading the generator when the client disconnects,
    but never closes it. A generator paused at a yield stays paused, and its
    finally block runs only when the garbage collector or a server shutdown
    reaches it. Found live on Week 5 Day 3: an interrupted answer was
    recorded 106 seconds late, at the next reload.

    Closing it here raises GeneratorExit at that yield, so its finally block
    runs as soon as the response ends. Closing a generator that has already
    finished does nothing.
    """

    media_type = "text/event-stream"

    def __init__(self, events: AsyncGenerator[str, None]) -> None:
        """Stream `events`, each already formatted by sse_event()."""
        # no-cache: each stream is one answer, never to be replayed.
        super().__init__(events, headers={"Cache-Control": "no-cache"})
        self._events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the stream, then close the generator however it ended."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._events.aclose()


def sse_event(event: str, data: BaseModel) -> str:
    """One server-sent event: its name, its data, and the blank line that
    ends it.

    The data is JSON on a single line. A raw newline would end the data
    line early; JSON writes newlines inside the text as \\n instead.
    """
    return f"event: {event}\ndata: {data.model_dump_json()}\n\n"

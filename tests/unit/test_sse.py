"""SSEResponse and sse_event (task 5.8), driven at the ASGI level.

The disconnect test reproduces the Week 5 Day 3 bug: Starlette stops
reading a stream when the client leaves but never closes the generator,
so a generator paused at a yield never ran its finally block.
"""

import anyio
from pydantic import BaseModel

from app.core.sse import SSEResponse, sse_event


class _Piece(BaseModel):
    text: str


async def test_a_generator_paused_at_a_yield_is_closed_when_the_client_disconnects() -> (
    None
):
    """The client leaves while the server is sending a piece, so the
    cancellation lands in send(), not inside the generator, which is left
    paused at its yield. SSEResponse must close it."""
    closed: list[bool] = []

    async def events():
        try:
            while True:
                yield sse_event("token", _Piece(text="a"))
        finally:
            closed.append(True)

    disconnected = anyio.Event()
    sent: list[dict] = []

    async def receive() -> dict:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)
        if len(sent) == 3:  # the response start, then two pieces
            disconnected.set()
            await anyio.sleep_forever()

    await SSEResponse(events())({"type": "http"}, receive, send)

    assert closed == [True]


async def test_the_response_is_an_uncached_event_stream() -> None:
    async def events():
        yield sse_event("done", _Piece(text="end"))

    sent: list[dict] = []

    async def receive() -> dict:
        await anyio.sleep_forever()
        return {}

    async def send(message: dict) -> None:
        sent.append(message)

    await SSEResponse(events())({"type": "http"}, receive, send)

    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"text/event-stream; charset=utf-8"
    assert headers[b"cache-control"] == b"no-cache"
    assert sent[1]["body"] == b'event: done\ndata: {"text":"end"}\n\n'


def test_an_event_keeps_its_data_on_one_line() -> None:
    """A raw newline would end the data line early; JSON escapes it."""
    event = sse_event("token", _Piece(text="Wear loose clothing.\n\nThis is not"))

    assert event == (
        'event: token\ndata: {"text":"Wear loose clothing.\\n\\nThis is not"}\n\n'
    )

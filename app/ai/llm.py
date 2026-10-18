"""Talks to the chat model, behind one small interface (task 5.1).

Everything that needs a reply from the model (the safety check, POST
/assistant/ask, staff generation) depends on LLMProvider and never on a
vendor. get_llm_provider() picks the implementation from settings:
OpenAICompatibleLLM for Groq or OpenAI, which take the same request format,
or FakeLLM, which the tests use so the suite needs no network and no key.

Unlike EmbeddingProvider, this interface is async. A model call spends
seconds waiting on the network and almost no CPU, and /assistant/ask streams
the reply out of an async generator. Awaiting the call lets the event loop
serve other requests, bookings included, in the meantime; a blocking call
would stall every request in the process until the model finished.

Error messages carry status codes, never the messages sent or the reply:
either can hold the patient's own words (rule 6.6).
"""

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal

import httpx

from app.core.config import settings

# The longest reply a call may produce unless the caller asks otherwise. It
# caps what one call can cost; a navigation answer is a few sentences.
DEFAULT_MAX_TOKENS = 512


class LLMError(Exception):
    """Base class for every model-call failure, so a caller can catch one type."""


class TransientLLMError(LLMError):
    """A failure that may succeed on retry: a timeout, a dropped connection,
    rate limiting (429), or a server error (5xx).
    """


class PermanentLLMError(LLMError):
    """A failure no retry can fix: a missing or rejected key, an unknown
    model, or a response that is not in the expected format.
    """


@dataclass(frozen=True)
class LLMMessage:
    """One message of the conversation sent to the model.

    "system" carries the instructions (task 5.2's prompts) and "user" the
    patient's question.
    """

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMUsage:
    """Token counts for one call, as the provider reported them.

    They are what the provider bills by, and ai_interactions (task 5.7)
    stores them beside each answer.
    """

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class LLMResult:
    """A whole reply, as complete() returns it."""

    text: str
    usage: LLMUsage


@dataclass(frozen=True)
class LLMChunk:
    """One piece of a streamed reply.

    `text` is the next fragment of the answer and may be empty. `usage` is
    set on the last chunk only: the provider counts tokens once the reply is
    finished, so a stream cut short never gets one.
    """

    text: str = ""
    usage: LLMUsage | None = None


class LLMProvider(ABC):
    """The contract every chat-model implementation keeps.

    An ABC, like EmbeddingProvider, so a subclass that forgets a method
    fails the moment it is constructed.
    """

    def __init__(self, model: str) -> None:
        """Record the model name. ai_interactions stores it beside each
        answer, so a wrong answer can be traced to the model that gave it.
        """
        self.model = model

    @abstractmethod
    async def complete(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> LLMResult:
        """Return the whole reply at once.

        For callers that need all of it before acting: the safety check,
        and a report that must be parsed as JSON.
        """

    @abstractmethod
    def stream(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> AsyncIterator[LLMChunk]:
        """Yield the reply piece by piece as the model writes it, then a
        last chunk carrying the token counts.

        For /assistant/ask, which passes each piece on to the patient
        (task 5.8).
        """


class OpenAICompatibleLLM(LLMProvider):
    """Calls a chat-completions endpoint in OpenAI's request format.

    Groq serves the same format at its own address, so one class covers
    both, and BASE_URLS maps LLM_PROVIDER to the address. A thin httpx
    client rather than a vendor SDK or LangChain: httpx is already a
    dependency, and the tests answer it in-process with httpx.MockTransport,
    as they do the hosted embedding provider.

    Each call opens its own connection and closes it when the call ends. A
    shared client would save a TLS handshake per call, tens of milliseconds
    against a reply of a second or more, but would have to be closed at
    shutdown and could not be shared between event loops, and every test
    runs in its own.
    """

    BASE_URLS = {
        "groq": "https://api.groq.com/openai/v1",
        "openai": "https://api.openai.com/v1",
    }

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_retries: int = 1,
        retry_delay_seconds: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Fail at construction if the key cannot be right, so the message
        names the real problem instead of every call failing with a 401.

        One retry by default: the patient is waiting for the answer, and a
        second failure in a row looks like an outage, not a blip.

        `transport` exists for the tests, which pass an httpx.MockTransport
        so no request leaves the process.
        """
        super().__init__(model)
        _check_api_key(api_key)
        self._url = f"{base_url}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._retry_delay = retry_delay_seconds
        self._transport = transport

    async def complete(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> LLMResult:
        """Send the request, retrying a transient failure up to
        `max_retries` times.
        """
        payload = self._payload(messages, max_tokens, stream=False)
        for attempt in range(self._max_retries + 1):
            try:
                return await self._complete_once(payload)
            except TransientLLMError:
                if attempt == self._max_retries:
                    raise
                await asyncio.sleep(self._retry_delay)
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def stream(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> AsyncIterator[LLMChunk]:
        """Yield the reply as it arrives, retrying a transient failure only
        while nothing has been yielded yet.

        Once a piece has reached the caller it may already be on the
        patient's screen, and a retry would send the start of the answer a
        second time. A failure after that point is raised for the caller to
        end the stream.
        """
        payload = self._payload(messages, max_tokens, stream=True)
        for attempt in range(self._max_retries + 1):
            yielded = False
            try:
                # aclosing() closes the inner stream, and with it the
                # connection, the moment this one is closed. That happens
                # when the patient disconnects mid-answer; without it the
                # provider would keep writing until the garbage collector
                # got round to the inner generator.
                async with aclosing(self._stream_once(payload)) as chunks:
                    async for chunk in chunks:
                        yielded = True
                        yield chunk
                return
            except TransientLLMError:
                if yielded or attempt == self._max_retries:
                    raise
                await asyncio.sleep(self._retry_delay)

    def _payload(
        self, messages: list[LLMMessage], max_tokens: int, *, stream: bool
    ) -> dict:
        """Build the request body both calls send."""
        payload: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            # 0 makes the model take its most likely word every time, so the
            # same question gets the same answer and a refusal that passes a
            # test once keeps passing.
            "temperature": 0,
            "max_completion_tokens": max_tokens,
        }
        if stream:
            payload["stream"] = True
            # Without this a streamed reply carries no token counts at all.
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _client(self) -> httpx.AsyncClient:
        """A client for one call. A float timeout applies to each step
        (connecting, and each wait for more of the reply), not to the whole
        call, so a long reply that keeps arriving is never cut off.
        """
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout)

    async def _complete_once(self, payload: dict) -> LLMResult:
        """Send one request and turn the response into an LLMResult."""
        try:
            async with self._client() as client:
                response = await client.post(
                    self._url, headers=self._headers, json=payload
                )
        except httpx.TransportError as exc:
            # Timeouts, refused connections and dropped connections.
            raise TransientLLMError(
                f"LLM provider unreachable ({type(exc).__name__})"
            ) from exc

        _raise_for_status(response.status_code)
        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"] or ""
            usage = _usage_from(body["usage"])
        except (ValueError, LookupError, TypeError) as exc:
            raise PermanentLLMError(
                "LLM provider returned a response in an unexpected format"
            ) from exc
        return LLMResult(text=text, usage=usage)

    async def _stream_once(self, payload: dict) -> AsyncIterator[LLMChunk]:
        """Send one streaming request and yield a chunk per fragment, then
        one chunk with the token counts.

        The reply arrives as server-sent events: a "data: {json}" line per
        fragment, then "data: [DONE]". Blank lines separate events, and a
        line starting with ":" is a keep-alive comment. Groq sends the
        token counts twice, on the event that ends the answer and again on
        a last event with no text, so they are held until the end and
        yielded once.
        """
        usage = None
        try:
            async with self._client() as client:
                async with client.stream(
                    "POST", self._url, headers=self._headers, json=payload
                ) as response:
                    _raise_for_status(response.status_code)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if data == "[DONE]":
                            break
                        chunk = _chunk_from(data)
                        if chunk.text:
                            yield LLMChunk(text=chunk.text)
                        usage = chunk.usage or usage
                    if usage is not None:
                        yield LLMChunk(usage=usage)
        except httpx.TransportError as exc:
            raise TransientLLMError(
                f"LLM provider unreachable ({type(exc).__name__})"
            ) from exc


def _check_api_key(api_key: str) -> None:
    """Raise PermanentLLMError if `api_key` cannot be a working key.

    The whitespace check is the case seen on Week 5 Day 1: with
    `LLM_API_KEY=` followed only by a comment, Docker Compose passes the
    comment's text as the key. A real key never contains a space.
    """
    if not api_key:
        raise PermanentLLMError("LLM_API_KEY is empty; set it in .env")
    if not api_key.isascii():
        raise PermanentLLMError(
            "LLM_API_KEY contains a non-ASCII character, which an HTTP header "
            "cannot carry; check its value in .env"
        )
    if any(char.isspace() for char in api_key):
        raise PermanentLLMError(
            "LLM_API_KEY contains a space; in .env, put the key alone on its "
            "line, with no comment after it"
        )


def _raise_for_status(status_code: int) -> None:
    """Raise the error that matches a failed HTTP status; return on 200.

    The response body is deliberately left out of the message: an error
    response may echo the request, and the request holds the patient's
    question.
    """
    if status_code == 200:
        return
    message = f"LLM provider returned HTTP {status_code}"
    if status_code == 429 or status_code >= 500:
        raise TransientLLMError(message)
    raise PermanentLLMError(message)


def _usage_from(usage: dict) -> LLMUsage:
    """Read the token counts from an OpenAI-format `usage` object."""
    return LLMUsage(
        input_tokens=usage["prompt_tokens"], output_tokens=usage["completion_tokens"]
    )


def _chunk_from(data: str) -> LLMChunk:
    """Turn one streamed event's JSON into an LLMChunk.

    Only `content` is read from a fragment. A reasoning model such as Groq's
    gpt-oss also streams its working under `reasoning`, which must never
    reach the patient: it is the model thinking aloud, and could hold the
    clinical speculation the assistant refuses to give.
    """
    try:
        event = json.loads(data)
    except ValueError as exc:
        raise PermanentLLMError(
            "LLM provider streamed an event that is not JSON"
        ) from exc
    try:
        if "error" in event:
            # A failure after the 200 status line can only arrive as an event.
            raise TransientLLMError("LLM provider reported an error mid-stream")
        text = ""
        if event.get("choices"):
            text = event["choices"][0]["delta"].get("content") or ""
        usage = event.get("usage")
        return LLMChunk(text=text, usage=_usage_from(usage) if usage else None)
    except (LookupError, TypeError, AttributeError) as exc:
        raise PermanentLLMError(
            "LLM provider streamed an event in an unexpected format"
        ) from exc


# A word and the spaces after it: "Knee X-Ray. Bring" splits into "Knee ",
# "X-Ray. " and "Bring", which join back to exactly the original.
_WORD_PIECE = re.compile(r"\S+\s*")


class FakeLLM(LLMProvider):
    """A network-free stand-in for tests.

    Replies with `reply`. stream() yields it one word per chunk, then a last
    chunk with the token counts, in the same order as the real provider.
    Tokens are counted as words: repeatable, though not what a real
    tokenizer would give.

    Records each call's messages so a test can check what the prompt held.
    With `error` set, every call raises it instead, so a test can drive the
    failure paths.
    """

    DEFAULT_REPLY = "This is a fake reply."

    def __init__(
        self, reply: str = DEFAULT_REPLY, *, error: LLMError | None = None
    ) -> None:
        """Start with an empty call log. The model name marks anything the
        fake produced, so it can never pass for a real answer in the
        database.
        """
        super().__init__(model="fake-llm-v1")
        self.reply = reply
        self.error = error
        self.calls: list[list[LLMMessage]] = []

    async def complete(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> LLMResult:
        """Record the call, then return the whole reply."""
        self._record(messages)
        return LLMResult(text=self.reply, usage=self._usage(messages))

    async def stream(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> AsyncIterator[LLMChunk]:
        """Record the call, then yield the reply a word at a time."""
        self._record(messages)
        for piece in _WORD_PIECE.findall(self.reply):
            # Hands control back to the event loop between pieces, as
            # waiting on a real provider does, so other work can run
            # between them.
            await asyncio.sleep(0)
            yield LLMChunk(text=piece)
        yield LLMChunk(usage=self._usage(messages))

    def _record(self, messages: list[LLMMessage]) -> None:
        """Log the call, then raise `error` if the test set one."""
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error

    def _usage(self, messages: list[LLMMessage]) -> LLMUsage:
        """Count words in the messages sent and in the reply."""
        return LLMUsage(
            input_tokens=sum(len(m.content.split()) for m in messages),
            output_tokens=len(self.reply.split()),
        )


def get_llm_provider() -> LLMProvider:
    """Build the provider LLM_PROVIDER names.

    The one place that reads the LLM settings, so swapping providers is a
    config change. Settings validation has already rejected any other
    value, so these two branches are complete.
    """
    if settings.llm_provider == "fake":
        return FakeLLM()
    return OpenAICompatibleLLM(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=OpenAICompatibleLLM.BASE_URLS[settings.llm_provider],
        model=settings.llm_model,
        timeout_seconds=settings.ai_request_timeout_seconds,
    )

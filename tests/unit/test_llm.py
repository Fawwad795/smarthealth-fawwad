"""Tests for app/ai/llm.py, with no network.

OpenAICompatibleLLM is exercised against httpx.MockTransport, which answers
inside the process, so these tests prove the request format, the stream
parsing, the retries and the transient/permanent split without a key or a
connection. The one test that calls Groq lives in
tests/integration/test_llm_live.py and is skipped unless asked for.
"""

import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.ai import llm
from app.ai.llm import (
    FakeLLM,
    LLMChunk,
    LLMMessage,
    LLMProvider,
    LLMResult,
    LLMUsage,
    OpenAICompatibleLLM,
    PermanentLLMError,
    TransientLLMError,
    get_llm_provider,
)
from app.core.config import Settings

PATIENT_TEXT = "a question in the patient's own words"
MESSAGES = [
    LLMMessage("system", "You are a navigation assistant."),
    LLMMessage("user", PATIENT_TEXT),
]
URL = "https://llm.test/v1/chat/completions"


def _llm(handler, *, max_retries: int = 1) -> OpenAICompatibleLLM:
    """A provider whose requests are answered by `handler`, in-process."""
    return OpenAICompatibleLLM(
        api_key="gsk_test",
        base_url="https://llm.test/v1",
        model="test-model",
        timeout_seconds=1,
        max_retries=max_retries,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )


def _completion(text: str) -> dict:
    """A whole reply in OpenAI's format, as Groq returns it."""
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
    }


def _delta(text: str) -> dict:
    """One streamed fragment of a reply."""
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


# The event that ends a stream asked for with include_usage: no choices,
# just the counts.
USAGE_EVENT = {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 2}}


def _sse(*events: dict) -> bytes:
    """Encode events as a streamed reply: a "data:" line each, then [DONE]."""
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return (body + "data: [DONE]\n\n").encode()


async def _collect(chunks) -> list[LLMChunk]:
    """Drain a stream into a list."""
    return [chunk async for chunk in chunks]


# --- the contract ---------------------------------------------------------


def test_a_provider_missing_stream_cannot_be_constructed() -> None:
    """The reason LLMProvider is an ABC: the mistake surfaces at
    construction, not on the first patient's question."""

    class Incomplete(LLMProvider):
        async def complete(self, messages, *, max_tokens=512):
            return LLMResult("", LLMUsage(0, 0))

    with pytest.raises(TypeError, match="stream"):
        Incomplete("m")


# --- FakeLLM --------------------------------------------------------------


async def test_fake_complete_returns_its_reply_and_records_the_messages() -> None:
    fake = FakeLLM("Knee X-Ray is offered.")

    result = await fake.complete(MESSAGES)

    assert result == LLMResult("Knee X-Ray is offered.", LLMUsage(12, 4))
    assert fake.calls == [MESSAGES]


async def test_fake_stream_yields_word_pieces_that_join_to_the_reply_then_counts() -> (
    None
):
    fake = FakeLLM("Knee X-Ray. Bring prior imaging.")

    chunks = await _collect(fake.stream(MESSAGES))

    *pieces, last = chunks
    assert [p.text for p in pieces] == [
        "Knee ",
        "X-Ray. ",
        "Bring ",
        "prior ",
        "imaging.",
    ]
    assert last == LLMChunk(text="", usage=LLMUsage(12, 5))
    assert fake.calls == [MESSAGES]


async def test_fake_raises_its_error_from_both_calls() -> None:
    fake = FakeLLM(error=TransientLLMError("down"))

    with pytest.raises(TransientLLMError):
        await fake.complete(MESSAGES)
    with pytest.raises(TransientLLMError):
        await _collect(fake.stream(MESSAGES))
    assert len(fake.calls) == 2


# --- OpenAICompatibleLLM: what it sends and reads -------------------------


async def test_complete_sends_the_messages_at_temperature_zero_with_the_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_completion("Hello there"))

    result = await _llm(handler).complete(MESSAGES, max_tokens=50)

    assert result == LLMResult(
        "Hello there", LLMUsage(input_tokens=12, output_tokens=5)
    )
    (request,) = requests
    assert str(request.url) == URL
    assert request.headers["authorization"] == "Bearer gsk_test"
    assert json.loads(request.content) == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "You are a navigation assistant."},
            {"role": "user", "content": PATIENT_TEXT},
        ],
        "temperature": 0,
        "max_completion_tokens": 50,
    }


async def test_stream_asks_for_token_counts_and_yields_pieces_in_order() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # A keep-alive comment and an empty fragment, which a provider may
        # send at any point, must be skipped rather than passed on.
        body = b": keep-alive\n\n" + _sse(
            _delta(""), _delta("Hel"), _delta("lo"), USAGE_EVENT
        )
        return httpx.Response(200, content=body)

    chunks = await _collect(_llm(handler).stream(MESSAGES))

    assert chunks == [
        LLMChunk(text="Hel"),
        LLMChunk(text="lo"),
        LLMChunk(usage=LLMUsage(input_tokens=12, output_tokens=2)),
    ]
    sent = json.loads(requests[0].content)
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}


async def test_token_counts_sent_twice_are_yielded_once_as_the_last_chunk() -> None:
    """Groq's real shape, seen live on Week 5 Day 1: the event that ends the
    answer carries the counts, and a last event with no choices repeats
    them. A caller adding up tokens would otherwise count them twice."""
    counts = {"prompt_tokens": 21, "completion_tokens": 3}
    finish = {
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": counts,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = _sse(_delta("hello"), finish, {"choices": [], "usage": counts})
        return httpx.Response(200, content=body)

    chunks = await _collect(_llm(handler).stream(MESSAGES))

    assert chunks == [
        LLMChunk(text="hello"),
        LLMChunk(usage=LLMUsage(input_tokens=21, output_tokens=3)),
    ]


# What a reasoning model (Groq's gpt-oss) writes before its answer, in the
# shape seen live on Week 5 Day 1: a `reasoning` field beside `content`.
REASONING = "The user wants a scan. Which department fits?"


async def test_a_reasoning_models_working_never_reaches_the_caller() -> None:
    whole = _completion("Radiology offers Knee X-Ray.")
    whole["choices"][0]["message"]["reasoning"] = REASONING
    streamed = _sse(
        {
            "choices": [
                {"index": 0, "delta": {"channel": "analysis", "reasoning": REASONING}}
            ]
        },
        _delta("Radiology offers Knee X-Ray."),
        USAGE_EVENT,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=streamed)
        return httpx.Response(200, json=whole)

    provider = _llm(handler)
    result = await provider.complete(MESSAGES)
    chunks = await _collect(provider.stream(MESSAGES))

    assert result.text == "Radiology offers Knee X-Ray."
    assert "".join(c.text for c in chunks) == "Radiology offers Knee X-Ray."


# --- OpenAICompatibleLLM: failures ----------------------------------------


def _echoing(status_code: int):
    """A handler failing with `status_code` and a body that echoes the
    patient's question, as a real error response may."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, json={"error": {"message": f"bad input: {PATIENT_TEXT}"}}
        )

    return handler


@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_rate_limits_and_server_errors_are_transient(status_code: int) -> None:
    with pytest.raises(TransientLLMError) as caught:
        await _llm(_echoing(status_code), max_retries=0).complete(MESSAGES)

    assert str(status_code) in str(caught.value)
    assert PATIENT_TEXT not in str(caught.value)


@pytest.mark.parametrize("status_code", [400, 401, 404])
async def test_a_rejected_key_or_request_is_permanent(status_code: int) -> None:
    with pytest.raises(PermanentLLMError) as caught:
        await _llm(_echoing(status_code), max_retries=0).complete(MESSAGES)

    assert str(status_code) in str(caught.value)
    assert PATIENT_TEXT not in str(caught.value)


async def test_an_unreachable_provider_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(TransientLLMError, match="ConnectError"):
        await _llm(handler, max_retries=0).complete(MESSAGES)


@pytest.mark.parametrize(
    "body", [b"<html>gateway</html>", b'{"unexpected": true}', b'{"choices": []}']
)
async def test_a_reply_in_an_unexpected_format_is_permanent(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(PermanentLLMError, match="unexpected format"):
        await _llm(handler).complete(MESSAGES)


async def test_a_stream_the_provider_refuses_raises_its_status() -> None:
    with pytest.raises(PermanentLLMError, match="401"):
        await _collect(_llm(_echoing(401)).stream(MESSAGES))


async def test_an_error_event_mid_stream_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _sse(_delta("Hel"), {"error": {"message": "overloaded"}})
        return httpx.Response(200, content=body)

    with pytest.raises(TransientLLMError, match="mid-stream"):
        await _collect(_llm(handler).stream(MESSAGES))


async def test_a_streamed_event_that_is_not_json_is_permanent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: {not json\n\n")

    with pytest.raises(PermanentLLMError, match="not JSON"):
        await _collect(_llm(handler).stream(MESSAGES))


@pytest.mark.parametrize(
    "event",
    [
        b'{"choices": [{"index": 0}]}',  # a choice with no delta
        b"[1, 2]",  # JSON, but not an object
    ],
)
async def test_a_streamed_event_in_an_unexpected_format_is_permanent(
    event: bytes,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: " + event + b"\n\n")

    with pytest.raises(PermanentLLMError, match="unexpected format"):
        await _collect(_llm(handler).stream(MESSAGES))


# --- retries --------------------------------------------------------------


def _replying(*responses: httpx.Response):
    """A handler returning `responses` in turn, and the list of requests it
    received."""
    queue = list(responses)
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return queue.pop(0)

    return handler, received


async def test_complete_retries_a_transient_failure_once() -> None:
    handler, requests = _replying(
        httpx.Response(503), httpx.Response(200, json=_completion("Hi"))
    )

    result = await _llm(handler, max_retries=1).complete(MESSAGES)

    assert result.text == "Hi"
    assert len(requests) == 2


async def test_complete_gives_up_after_its_retries() -> None:
    handler, requests = _replying(httpx.Response(503), httpx.Response(503))

    with pytest.raises(TransientLLMError):
        await _llm(handler, max_retries=1).complete(MESSAGES)
    assert len(requests) == 2


async def test_complete_does_not_retry_a_permanent_failure() -> None:
    handler, requests = _replying(httpx.Response(401), httpx.Response(200))

    with pytest.raises(PermanentLLMError):
        await _llm(handler, max_retries=1).complete(MESSAGES)
    assert len(requests) == 1


async def test_stream_retries_a_failure_before_its_first_piece() -> None:
    handler, requests = _replying(
        httpx.Response(503),
        httpx.Response(200, content=_sse(_delta("Hi"), USAGE_EVENT)),
    )

    chunks = await _collect(_llm(handler, max_retries=1).stream(MESSAGES))

    assert [c.text for c in chunks] == ["Hi", ""]
    assert len(requests) == 2


async def test_stream_does_not_retry_once_a_piece_has_been_yielded() -> None:
    """A retry here would send the start of the answer twice."""

    async def dropped_mid_reply():
        yield f"data: {json.dumps(_delta('Hel'))}\n\n".encode()
        raise httpx.ReadError("connection reset")

    handler, requests = _replying(
        httpx.Response(200, content=dropped_mid_reply()),
        httpx.Response(200, content=_sse(_delta("Hello"), USAGE_EVENT)),
    )
    received: list[LLMChunk] = []

    with pytest.raises(TransientLLMError, match="ReadError"):
        async for chunk in _llm(handler, max_retries=1).stream(MESSAGES):
            received.append(chunk)

    assert received == [LLMChunk(text="Hel")]
    assert len(requests) == 1


async def test_abandoning_a_stream_closes_the_connection_to_the_provider() -> None:
    """What happens when the patient disconnects mid-answer (task 5.8): the
    provider's response is closed at once, not when the garbage collector
    gets to it, so the model stops writing a reply nobody will read."""
    responses: list[httpx.Response] = []

    async def endless_reply():
        # A generator, not bytes: httpx reads a bytes body in full when the
        # Response is built, which would close it before the test began.
        while True:
            yield f"data: {json.dumps(_delta('word '))}\n\n".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, content=endless_reply())
        responses.append(response)
        return response

    stream = _llm(handler).stream(MESSAGES)
    first = await anext(stream)
    await stream.aclose()

    assert first == LLMChunk(text="word ")
    assert responses[0].is_closed


# --- the key --------------------------------------------------------------


@pytest.mark.parametrize(
    ("api_key", "problem"),
    [
        ("", "empty"),
        ("gsk_tëst", "non-ASCII"),
        # What Docker Compose passes for `LLM_API_KEY=  # a comment`.
        ("# paste in .env only, never here", "space"),
    ],
)
def test_a_key_that_cannot_work_fails_at_construction(
    api_key: str, problem: str
) -> None:
    with pytest.raises(PermanentLLMError, match=problem):
        OpenAICompatibleLLM(
            api_key=api_key,
            base_url="https://llm.test/v1",
            model="m",
            timeout_seconds=1,
        )


# --- choosing the provider ------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("groq", "https://api.groq.com/openai/v1/chat/completions"),
        ("openai", "https://api.openai.com/v1/chat/completions"),
    ],
)
def test_factory_builds_the_hosted_provider_from_settings(
    monkeypatch: pytest.MonkeyPatch, provider: str, url: str
) -> None:
    monkeypatch.setattr(llm.settings, "llm_provider", provider)
    monkeypatch.setattr(llm.settings, "llm_api_key", SecretStr("key_x"))
    monkeypatch.setattr(llm.settings, "llm_model", "test-model")

    built = get_llm_provider()

    assert isinstance(built, OpenAICompatibleLLM)
    assert built.model == "test-model"
    assert built._url == url


def test_settings_reject_a_provider_the_client_cannot_speak() -> None:
    """Anthropic's API has a different shape; naming it must fail at
    startup, not on the first question."""
    with pytest.raises(ValidationError, match="llm_provider"):
        Settings(llm_provider="anthropic")

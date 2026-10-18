"""The one test that calls Groq for real.

Skipped by default: the suite must pass with no network and no key. Run it
on purpose with

    docker compose run --rm -e RUN_LIVE_TESTS=1 test pytest -m live

It proves what MockTransport cannot: that Groq's real endpoint, model and
reply format still match what OpenAICompatibleLLM expects, including the
token counts at the end of a stream.
"""

import os

import pytest

from app.ai import llm
from app.ai.llm import LLMMessage, OpenAICompatibleLLM, get_llm_provider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="calls Groq; set RUN_LIVE_TESTS=1 to run",
    ),
]

MESSAGES = [LLMMessage("user", "Reply with exactly these two words: hello there")]


async def test_groq_answers_whole_and_streamed_with_token_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fake_llm_by_default has switched every test to the fake.
    monkeypatch.setattr(llm.settings, "llm_provider", "groq")
    provider = get_llm_provider()
    assert isinstance(provider, OpenAICompatibleLLM)

    whole = await provider.complete(MESSAGES, max_tokens=10)
    chunks = [chunk async for chunk in provider.stream(MESSAGES, max_tokens=10)]

    assert "hello" in whole.text.lower()
    assert whole.usage.input_tokens > 0 and whole.usage.output_tokens > 0
    assert "hello" in "".join(chunk.text for chunk in chunks).lower()
    # include_usage working: only the last chunk carries the counts.
    assert chunks[-1].usage is not None and chunks[-1].usage.output_tokens > 0
    assert all(chunk.usage is None for chunk in chunks[:-1])

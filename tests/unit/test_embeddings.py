"""Tests for app/ai/embeddings.py, with no network.

The Hugging Face provider is exercised against httpx.MockTransport, which
answers inside the process, so these tests prove the batching and the
transient/permanent split without a key or a connection. The one test that
does call Hugging Face lives in tests/integration/test_embeddings_live.py
and is skipped unless asked for.
"""

import json
import math

import httpx
import pytest
from pydantic import SecretStr

from app.ai import embeddings
from app.ai.embeddings import (
    EmbeddingProvider,
    FakeEmbeddings,
    HuggingFaceEmbeddings,
    PermanentEmbeddingError,
    TransientEmbeddingError,
    get_embedding_provider,
)

PATIENT_TEXT = "a question in the patient's own words"


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    )


def _hf(handler, *, batch_size: int = 64, dimensions: int = 3) -> HuggingFaceEmbeddings:
    """A provider whose HTTP requests are answered by `handler`, in-process."""
    return HuggingFaceEmbeddings(
        api_key="hf_test",
        model="test/model",
        dimensions=dimensions,
        batch_size=batch_size,
        timeout_seconds=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


# --- the contract ---------------------------------------------------------


def test_a_provider_missing_embed_cannot_be_constructed() -> None:
    """The reason EmbeddingProvider is an ABC: the mistake surfaces at
    construction, not on the first publish."""

    class Incomplete(EmbeddingProvider):
        pass

    with pytest.raises(TypeError, match="embed"):
        Incomplete("m", 3)


# --- FakeEmbeddings -------------------------------------------------------


def test_fake_vectors_are_unit_length_and_repeatable() -> None:
    first = FakeEmbeddings().embed(["Knee X-Ray"])[0]
    second = FakeEmbeddings().embed(["Knee X-Ray"])[0]

    assert first == second
    assert len(first) == 384
    assert math.isclose(math.sqrt(sum(x * x for x in first)), 1.0)


def test_fake_vectors_are_pinned_across_processes() -> None:
    """ "knee" always lands at position 216. With the built-in hash(), which
    is salted per process, this would change between test runs."""
    vector = FakeEmbeddings().embed(["knee"])[0]

    assert vector.index(1.0) == 216


def test_fake_texts_sharing_words_are_more_similar() -> None:
    query, knee, skin = FakeEmbeddings().embed(
        ["knee scan", "Knee X-Ray", "Full Skin Check"]
    )

    assert _cosine(query, knee) > _cosine(query, skin)
    assert _cosine(query, skin) == 0.0


def test_fake_text_without_words_still_has_a_direction() -> None:
    """pgvector returns NaN for the cosine distance to an all-zero vector."""
    for text in ["", "?!"]:
        vector = FakeEmbeddings().embed([text])[0]
        assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0)


def test_fake_records_each_batch_it_was_asked_for() -> None:
    fake = FakeEmbeddings()
    fake.embed(["a", "b"])
    fake.embed(["c"])

    assert fake.calls == [["a", "b"], ["c"]]


# --- HuggingFaceEmbeddings ------------------------------------------------


def test_hf_sends_batches_and_keeps_the_input_order() -> None:
    """Five texts at batch size 2 is three requests, never five."""
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer hf_test"
        texts = json.loads(request.content)["inputs"]
        seen.append(texts)
        # Each vector encodes its text's number, so order is checkable.
        return httpx.Response(200, json=[[float(t[-1]), 0.0, 0.0] for t in texts])

    vectors = _hf(handler, batch_size=2).embed(["t1", "t2", "t3", "t4", "t5"])

    assert seen == [["t1", "t2"], ["t3", "t4"], ["t5"]]
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0, 4.0, 5.0]


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_hf_rate_limits_and_server_errors_are_transient(status_code: int) -> None:
    provider = _hf(lambda request: httpx.Response(status_code, json={"error": "x"}))

    with pytest.raises(TransientEmbeddingError, match=str(status_code)):
        provider.embed([PATIENT_TEXT])


def test_hf_a_connection_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("handshake timed out", request=request)

    with pytest.raises(TransientEmbeddingError, match="ConnectTimeout"):
        _hf(handler).embed([PATIENT_TEXT])


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_hf_client_errors_are_permanent(status_code: int) -> None:
    provider = _hf(lambda request: httpx.Response(status_code, json={"error": "x"}))

    with pytest.raises(PermanentEmbeddingError, match=str(status_code)):
        provider.embed([PATIENT_TEXT])


def test_hf_an_error_message_never_contains_the_input_text() -> None:
    """Even when the error body echoes the request, the message must not:
    a search query is patient text, and messages end up in logs."""
    provider = _hf(
        lambda request: httpx.Response(422, json={"error": f"bad input {PATIENT_TEXT}"})
    )

    with pytest.raises(PermanentEmbeddingError) as exc_info:
        provider.embed([PATIENT_TEXT])

    assert PATIENT_TEXT not in str(exc_info.value)


@pytest.mark.parametrize(
    "body",
    [
        [[0.1, 0.2]],  # wrong vector size
        [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]],  # more vectors than texts
        [[[0.1, 0.2, 0.3]]],  # one vector per token, not per text
        {"error": "not a list"},
    ],
)
def test_hf_a_wrongly_shaped_response_is_permanent(body: object) -> None:
    provider = _hf(lambda request: httpx.Response(200, json=body))

    with pytest.raises(PermanentEmbeddingError, match="expected"):
        provider.embed(["one text"])


def test_hf_a_success_status_with_a_non_json_body_is_permanent() -> None:
    """A 200 carrying an HTML page means the endpoint contract changed."""
    provider = _hf(lambda request: httpx.Response(200, content=b"<html>moved</html>"))

    with pytest.raises(PermanentEmbeddingError, match="non-JSON"):
        provider.embed(["one text"])


def test_hf_refuses_to_start_without_a_key() -> None:
    with pytest.raises(PermanentEmbeddingError, match="EMBEDDING_API_KEY"):
        HuggingFaceEmbeddings(
            api_key="", model="m", dimensions=3, batch_size=1, timeout_seconds=1
        )


# --- get_embedding_provider -----------------------------------------------


def test_factory_builds_the_fake_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings.settings, "embedding_provider", "fake")
    monkeypatch.setattr(embeddings.settings, "embedding_dimensions", 16)

    provider = get_embedding_provider()

    assert isinstance(provider, FakeEmbeddings)
    assert provider.dimensions == 16


def test_factory_builds_hugging_face_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction makes no request, so this needs no network."""
    monkeypatch.setattr(embeddings.settings, "embedding_provider", "huggingface")
    monkeypatch.setattr(embeddings.settings, "embedding_api_key", SecretStr("hf_x"))
    monkeypatch.setattr(embeddings.settings, "embedding_model", "test/model")
    monkeypatch.setattr(embeddings.settings, "embedding_dimensions", 384)

    provider = get_embedding_provider()

    assert isinstance(provider, HuggingFaceEmbeddings)
    assert (provider.model, provider.dimensions) == ("test/model", 384)

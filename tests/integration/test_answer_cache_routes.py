"""The answer cache (task 5.12), through POST /assistant/ask and
/ask/stream.

FakeLLM records every call, so "served from the cache" is checked as "the
model was not called". The client fixture empties the test Redis before each
test.
"""

import logging
from collections.abc import Callable

import pytest
import redis
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.embeddings import FakeEmbeddings
from app.ai.llm import FakeLLM
from app.ai.prompts import PROMPT_NAV_V1, PROMPT_SAFETY_V1
from app.ai.retrieval import get_query_embeddings
from app.core.config import settings
from app.core.redis import get_redis
from app.core.security import create_access_token
from app.main import app
from app.models import AIInteraction, Department, Service, User
from app.models.enums import UserRole
from app.services.assistant import get_assistant_llm
from app.services.service_publish import unpublish_service
from tests.indexing import index_service
from tests.streaming import BreaksMidStream, read_events, streamed_text

ASK = "/api/v1/assistant/ask"
ASK_STREAM = "/api/v1/assistant/ask/stream"
THRESHOLD = 0.3
KNEE_TEXT = "Orthopaedics: Knee X-Ray. Imaging of the knee joint."


@pytest.fixture(autouse=True)
def _fixed_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "retrieval_min_similarity", THRESHOLD)


@pytest.fixture()
def use_llm(client: TestClient) -> Callable[[FakeLLM], FakeLLM]:
    """Make the assistant talk to the given FakeLLM, and embed with
    FakeEmbeddings. The client fixture clears both overrides."""
    app.dependency_overrides[get_query_embeddings] = lambda: FakeEmbeddings()

    def _use(llm: FakeLLM) -> FakeLLM:
        app.dependency_overrides[get_assistant_llm] = lambda: llm
        return llm

    return _use


@pytest.fixture()
def knee(db_session: Session, department: Department) -> Service:
    return index_service(
        db_session,
        department,
        "Knee X-Ray",
        KNEE_TEXT,
        description="Standard imaging of the knee joint.",
        prep_instructions="Wear loose clothing.",
    )


@pytest.fixture()
def other_patient_headers(db_session: Session) -> dict[str, str]:
    """A second synthetic patient: the cache is shared between patients."""
    user = User(
        email="cache.other.patient@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(user)
    db_session.flush()
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _ask(client: TestClient, headers: dict, question: str = "knee x-ray") -> dict:
    response = client.post(ASK, headers=headers, json={"question": question})
    assert response.status_code == 200
    return response.json()


def _rows(db: Session) -> list[AIInteraction]:
    """Every ai_interactions row the test's requests wrote, oldest first."""
    return list(db.scalars(select(AIInteraction).order_by(AIInteraction.id)))


def _cache_lookups(result: str) -> float:
    return REGISTRY.get_sample_value("ai_answer_cache_total", {"result": result})


def test_the_next_patient_asking_the_same_question_gets_the_cached_answer(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict[str, str],
    other_patient_headers: dict[str, str],
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """Case, spacing and the closing "?" do not matter. The hit calls no
    model, and its row records what wrote the reply, with no tokens."""
    llm = use_llm(FakeLLM("navigation", "Book the Knee X-Ray."))
    hits_before = _cache_lookups("hit")

    first = _ask(client, patient_auth_headers)
    second = _ask(client, other_patient_headers, "  Knee   X-RAY? ")

    assert len(llm.calls) == 2  # the first question's safety check and answer
    assert second["answer"] == first["answer"]
    assert (second["outcome"], second["intent"]) == ("ANSWERED", "NAVIGATION")
    assert [c["service_id"] for c in second["citations"]] == [knee.id]
    first_row, hit_row = _rows(db_session)
    assert (first_row.cache_hit, hit_row.cache_hit) == (False, True)
    assert (hit_row.input_tokens, hit_row.output_tokens) == (0, 0)
    assert hit_row.model == "fake-llm-v1"
    assert hit_row.safety_prompt_version == PROMPT_SAFETY_V1.version
    assert hit_row.prompt_version == PROMPT_NAV_V1.version
    assert hit_row.retrieved_ids["services"] == [knee.id]
    assert _cache_lookups("hit") - hits_before == 1


def test_a_refusal_is_cached_and_still_counted_as_a_refusal(
    client: TestClient,
    patient_auth_headers: dict[str, str],
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    llm = use_llm(FakeLLM("medical_advice"))
    labels = {"intent": "MEDICAL_ADVICE"}
    before = REGISTRY.get_sample_value("ai_refusals_total", labels)

    first = _ask(client, patient_auth_headers, "diagnose my knee x-ray")
    second = _ask(client, patient_auth_headers, "diagnose my knee x-ray")

    assert len(llm.calls) == 1
    assert first["answer"] == second["answer"]
    assert second["outcome"] == "REFUSED"
    assert REGISTRY.get_sample_value("ai_refusals_total", labels) - before == 2


def test_a_preparation_answer_is_never_cached(
    client: TestClient,
    patient_auth_headers: dict[str, str],
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
    test_redis_client: redis.Redis,
) -> None:
    """It is built from the caller's own appointments, so the next patient
    must never be given it: both questions go to the model."""
    # FakeLLM repeats its last reply once the list runs out: both
    # questions need their own label and answer.
    llm = use_llm(
        FakeLLM("preparation", "Wear loose clothing.", "preparation", "Wear.")
    )

    _ask(client, patient_auth_headers, "how do I prepare for my knee x-ray")
    _ask(client, patient_auth_headers, "how do I prepare for my knee x-ray")

    assert len(llm.calls) == 4
    assert test_redis_client.keys("ai-answer:*") == []


def test_a_reply_cut_short_is_not_cached(
    client: TestClient,
    patient_auth_headers: dict[str, str],
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """The provider fails part-way through a streamed answer. Only "Knee "
    was sent, so nothing is stored: the next question goes to the model and
    gets a whole answer."""
    use_llm(BreaksMidStream("navigation"))
    events = read_events(
        client.post(
            ASK_STREAM, headers=patient_auth_headers, json={"question": "knee x-ray"}
        )
    )
    assert [name for name, _ in events] == ["token", "error", "done"]
    assert events[-1][1]["outcome"] == "FAILED"

    llm = use_llm(FakeLLM("navigation", "Book the Knee X-Ray."))
    body = _ask(client, patient_auth_headers)

    assert len(llm.calls) == 2
    assert "Book the Knee X-Ray." in body["answer"]


def test_editing_a_service_misses_the_cache(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict[str, str],
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """What the prompt shows of each service is in the key, so the new
    description reaches the model rather than the old answer the patient."""
    llm = use_llm(FakeLLM("navigation", "Book.", "navigation", "Book."))
    _ask(client, patient_auth_headers)

    knee.description = "Weight-bearing imaging of both knees."
    db_session.flush()
    _ask(client, patient_auth_headers)

    assert len(llm.calls) == 4
    assert "Weight-bearing imaging of both knees." in llm.calls[3][-1].content


def test_an_unpublished_service_is_never_recommended_from_the_cache(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict[str, str],
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """Retrieval no longer finds it, so the key changes: the patient hears
    "we don't offer that", not the cached recommendation."""
    use_llm(FakeLLM("navigation", "Book the Knee X-Ray.", "navigation"))
    assert "Book the Knee X-Ray." in _ask(client, patient_auth_headers)["answer"]

    unpublish_service(db_session, knee.id)
    body = _ask(client, patient_auth_headers)

    assert body["outcome"] == "NOT_OFFERED"
    assert "Knee X-Ray" not in body["answer"]
    assert body["citations"] == []


def test_the_stream_sends_a_cached_answer_as_one_piece(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict[str, str],
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """/ask and /ask/stream share the cache. Nothing is being generated, so
    the reply is one token event, then citations and done."""
    llm = use_llm(FakeLLM("navigation", "Book the Knee X-Ray."))
    answer = _ask(client, patient_auth_headers)["answer"]

    response = client.post(
        ASK_STREAM, headers=patient_auth_headers, json={"question": "knee x-ray"}
    )

    events = read_events(response)
    assert [name for name, _ in events] == ["token", "citations", "done"]
    assert streamed_text(events) == answer
    assert len(llm.calls) == 2
    assert _rows(db_session)[-1].cache_hit is True


def test_a_hit_does_not_extend_the_entrys_time_to_live(
    client: TestClient,
    patient_auth_headers: dict[str, str],
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
    test_redis_client: redis.Redis,
) -> None:
    """Stored for the TTL; a hit leaves the clock running, so a cached reply
    is never older than the TTL."""
    use_llm(FakeLLM("navigation", "Book the Knee X-Ray."))
    _ask(client, patient_auth_headers)
    (key,) = test_redis_client.keys("ai-answer:*")
    assert settings.ai_answer_cache_ttl_seconds - 5 < test_redis_client.ttl(key)

    test_redis_client.expire(key, 100)
    _ask(client, patient_auth_headers)

    assert test_redis_client.ttl(key) <= 100


def test_when_redis_is_down_the_question_is_answered_and_nothing_is_stored(
    client: TestClient,
    patient_auth_headers: dict[str, str],
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The lookup fails, is logged and counted, and the question goes to
    the model. No store is tried: Redis has just failed."""
    unreachable = redis.Redis(host="redis", port=1, socket_connect_timeout=1)
    app.dependency_overrides[get_redis] = lambda: unreachable
    llm = use_llm(FakeLLM("navigation", "Book.", "navigation", "Book."))
    caplog.set_level(logging.WARNING, logger="app.services.answer_cache")
    errors_before = _cache_lookups("error")

    _ask(client, patient_auth_headers)
    _ask(client, patient_auth_headers)

    assert len(llm.calls) == 4
    assert _cache_lookups("error") - errors_before == 2
    messages = [
        r.getMessage() for r in caplog.records if r.name == "app.services.answer_cache"
    ]
    assert (
        messages
        == ["answer cache lookup failed: redis unavailable (ConnectionError)"] * 2
    )

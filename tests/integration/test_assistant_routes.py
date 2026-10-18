"""POST /assistant/ask through the real FastAPI routing layer (task 5.3).

The model is a FakeLLM scripted per test through app.dependency_overrides:
its first reply is the safety label, its second the answer. Retrieval runs
for real against services indexed with FakeEmbeddings, which ranks by
shared words, so "knee x-ray" finds the Knee X-Ray service and "hair
transplant" finds nothing. The threshold is fixed at 0.3, as in the search
tests.
"""

import logging
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import llm as llm_module
from app.ai.embeddings import EmbeddingProvider, FakeEmbeddings, TransientEmbeddingError
from app.ai.llm import (
    DEFAULT_MAX_TOKENS,
    FakeLLM,
    LLMMessage,
    LLMResult,
    TransientLLMError,
)
from app.ai.prompts import DISCLAIMER, NO_MATCH_V1, PROMPT_NAV_V1
from app.ai.retrieval import get_query_embeddings
from app.core.config import settings
from app.main import app
from app.models import AIInteraction, Department, Service, User
from app.models.enums import ServiceStatus
from app.services.assistant import get_assistant_llm
from tests.indexing import index_service

THRESHOLD = 0.3

KNEE_TEXT = "Orthopaedics: Knee X-Ray. Imaging of the knee joint."
SKIN_TEXT = "Dermatology: Skin Check. A look at moles and rashes."


@pytest.fixture(autouse=True)
def _fixed_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "retrieval_min_similarity", THRESHOLD)


@pytest.fixture()
def fake_search(client: TestClient) -> None:
    """Embed questions with FakeEmbeddings. The client fixture clears
    every override when the test ends."""
    app.dependency_overrides[get_query_embeddings] = lambda: FakeEmbeddings()


@pytest.fixture()
def use_llm(client: TestClient, fake_search: None) -> Callable[[FakeLLM], FakeLLM]:
    """Make POST /assistant/ask talk to the given FakeLLM, and return it."""

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
def skin(db_session: Session, department: Department) -> Service:
    return index_service(db_session, department, "Skin Check", SKIN_TEXT)


def _ask(client: TestClient, headers: dict, question: str):
    return client.post(
        "/api/v1/assistant/ask", headers=headers, json={"question": question}
    )


def _interaction(db: Session) -> AIInteraction:
    """The one ai_interactions row the request wrote."""
    return db.scalars(select(AIInteraction)).one()


def _words(calls: list[list[LLMMessage]]) -> int:
    """FakeLLM's input token count over every call: words sent."""
    return sum(len(m.content.split()) for call in calls for m in call)


# --- answering ---------------------------------------------------------------


def test_ask_answers_from_the_services_found_and_cites_them(
    client: TestClient,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    department: Department,
    knee: Service,
    skin: Service,
) -> None:
    use_llm(FakeLLM("navigation", "Knee X-Ray fits. Wear loose clothing."))

    response = _ask(client, patient_auth_headers, "knee x-ray")

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == f"Knee X-Ray fits. Wear loose clothing.\n\n{DISCLAIMER}"
    assert body["outcome"] == "ANSWERED"
    assert body["intent"] == "NAVIGATION"
    assert body["citations"] == [
        {
            "service_id": knee.id,
            "name": "Knee X-Ray",
            "department": department.name,
            "similarity": 0.696,
        }
    ]


def test_the_answer_prompt_holds_only_the_services_found(
    client: TestClient,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
    skin: Service,
) -> None:
    """The model sees the knee service's description and preparation, and
    nothing about the skin check, which retrieval did not find."""
    llm = use_llm(FakeLLM("navigation", "Knee X-Ray fits."))

    _ask(client, patient_auth_headers, "knee x-ray")

    _, (system, user) = llm.calls
    assert system.content == PROMPT_NAV_V1.text.format(
        clinic=settings.assistant_clinic_name
    )
    assert "- Knee X-Ray (" in user.content
    assert "Description: Standard imaging of the knee joint." in user.content
    assert "Preparation: Wear loose clothing." in user.content
    assert "Skin Check" not in user.content
    assert user.content.endswith("<question>\nknee x-ray\n</question>")


def test_a_service_with_no_notes_is_shown_as_none_listed(
    client: TestClient,
    db_session: Session,
    department: Department,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    """A missing description or preparation is said to be missing, never
    left for the model to fill in."""
    index_service(db_session, department, "Knee X-Ray", KNEE_TEXT)
    llm = use_llm(FakeLLM("navigation", "Knee X-Ray fits."))

    _ask(client, patient_auth_headers, "knee x-ray")

    user = llm.calls[1][1].content
    assert "Description: None listed." in user
    assert "Preparation: None listed." in user


def test_an_unpublished_service_is_never_shown_to_the_model_or_cited(
    client: TestClient,
    db_session: Session,
    department: Department,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """Same text as the published knee service, so it would score the same."""
    index_service(
        db_session,
        department,
        "Old Knee X-Ray",
        KNEE_TEXT,
        status=ServiceStatus.INACTIVE,
    )
    llm = use_llm(FakeLLM("navigation", "Knee X-Ray fits."))

    response = _ask(client, patient_auth_headers, "knee x-ray")

    assert [c["service_id"] for c in response.json()["citations"]] == [knee.id]
    assert "Old Knee X-Ray" not in llm.calls[1][1].content


def test_an_other_question_is_answered_like_navigation(
    client: TestClient,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """Questions labelled other go through retrieval too: the threshold
    decides whether the clinic offers anything that fits."""
    llm = use_llm(FakeLLM("other", "Knee X-Ray fits."))

    response = _ask(client, patient_auth_headers, "knee x-ray")

    assert response.json()["outcome"] == "ANSWERED"
    assert response.json()["intent"] == "OTHER"
    assert len(llm.calls) == 2


def test_ask_records_the_interaction_with_ids_and_tokens(
    client: TestClient,
    db_session: Session,
    patient_user: User,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    llm = use_llm(FakeLLM("navigation", "Knee X-Ray fits. Wear loose clothing."))

    body = _ask(client, patient_auth_headers, "knee x-ray").json()

    row = _interaction(db_session)
    assert body["interaction_id"] == row.id
    assert row.user_id == patient_user.id
    assert row.outcome == "ANSWERED"
    assert row.intent == "NAVIGATION"
    assert row.retrieved_ids == {"services": [knee.id], "appointments": []}
    assert row.model == "fake-llm-v1"
    assert row.safety_prompt_version == "PROMPT_SAFETY_V1"
    assert row.prompt_version == "PROMPT_NAV_V1"
    assert row.input_tokens == _words(llm.calls)
    assert row.output_tokens == 1 + 6
    assert row.answer_chars == len(body["answer"])
    assert row.latency_ms >= 0


# --- refusing and not offering -----------------------------------------------


def test_a_diagnose_me_question_is_refused_and_routed_with_the_disclaimer(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    department: Department,
    knee: Service,
) -> None:
    """The refusal names the service retrieval found, and the answer prompt
    is never sent: a refused question gets fixed text only."""
    llm = use_llm(FakeLLM("medical_advice", "An answer that must never be asked for."))

    response = _ask(client, patient_auth_headers, "diagnose my knee x-ray")

    body = response.json()
    assert response.status_code == 200
    assert body["answer"].startswith("I can't give medical advice")
    assert f"Knee X-Ray ({department.name})" in body["answer"]
    assert "urgent care" in body["answer"]
    assert body["answer"].endswith(DISCLAIMER)
    assert body["outcome"] == "REFUSED"
    assert body["intent"] == "MEDICAL_ADVICE"
    assert [c["service_id"] for c in body["citations"]] == [knee.id]
    assert len(llm.calls) == 1
    row = _interaction(db_session)
    assert row.outcome == "REFUSED"
    assert row.prompt_version == "REFUSAL_V1"


def test_a_reply_that_names_no_label_is_refused(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    llm = use_llm(FakeLLM("I am not sure what this is."))

    body = _ask(client, patient_auth_headers, "knee x-ray").json()

    assert body["outcome"] == "REFUSED"
    assert body["intent"] is None
    assert len(llm.calls) == 1
    assert _interaction(db_session).intent is None


def test_a_question_nothing_offered_matches_gets_we_dont_offer_that(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    llm = use_llm(FakeLLM("navigation", "An answer that must never be asked for."))

    body = _ask(client, patient_auth_headers, "hair transplant").json()

    no_match = NO_MATCH_V1.text.format(clinic=settings.assistant_clinic_name)
    assert body["answer"] == f"{no_match}\n\n{DISCLAIMER}"
    assert body["outcome"] == "NOT_OFFERED"
    assert body["citations"] == []
    assert len(llm.calls) == 1
    row = _interaction(db_session)
    assert row.prompt_version == "NO_MATCH_V1"
    assert row.retrieved_ids == {"services": [], "appointments": []}


# --- bad requests and access -------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [{}, {"question": "   "}, {"question": "!!!!"}, {"question": "x" * 1001}],
    ids=["missing", "spaces", "no-letters", "too-long"],
)
def test_a_bad_question_is_422_before_any_ai_work(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    body: dict,
) -> None:
    llm = use_llm(FakeLLM("navigation"))

    response = client.post(
        "/api/v1/assistant/ask", headers=patient_auth_headers, json=body
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert llm.calls == []
    assert db_session.scalars(select(AIInteraction)).all() == []


def test_ask_requires_sign_in(client: TestClient) -> None:
    response = client.post("/api/v1/assistant/ask", json={"question": "knee x-ray"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "headers",
    ["provider_auth_headers", "front_desk_auth_headers", "admin_auth_headers"],
)
def test_ask_is_for_patients_only(
    client: TestClient,
    use_llm: Callable[[FakeLLM], FakeLLM],
    request: pytest.FixtureRequest,
    headers: str,
) -> None:
    llm = use_llm(FakeLLM("navigation"))

    response = _ask(client, request.getfixturevalue(headers), "knee x-ray")

    assert response.status_code == 403
    assert llm.calls == []


# --- failures ----------------------------------------------------------------


def test_ask_is_503_and_records_a_failure_when_the_safety_check_fails(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """With no safety check there is no answer."""
    use_llm(FakeLLM(TransientLLMError("LLM provider returned HTTP 503")))

    response = _ask(client, patient_auth_headers, "knee x-ray")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ASSISTANT_UNAVAILABLE"
    assert "HTTP 503" not in response.text
    row = _interaction(db_session)
    assert row.outcome == "FAILED"
    assert row.intent is None
    assert row.input_tokens == 0


def test_ask_is_503_when_the_answer_fails_and_keeps_the_safety_checks_tokens(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    llm = use_llm(
        FakeLLM("navigation", TransientLLMError("LLM provider returned HTTP 503"))
    )

    response = _ask(client, patient_auth_headers, "knee x-ray")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ASSISTANT_UNAVAILABLE"
    row = _interaction(db_session)
    assert row.outcome == "FAILED"
    assert row.intent == "NAVIGATION"
    assert row.prompt_version == "PROMPT_NAV_V1"
    assert row.retrieved_ids["services"] == [knee.id]
    assert row.input_tokens == _words(llm.calls[:1])
    assert row.output_tokens == 1


def test_ask_is_503_when_the_model_cannot_be_built(
    client: TestClient,
    patient_auth_headers: dict,
    fake_search: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real dependency, with Groq and no key."""
    monkeypatch.setattr(llm_module.settings, "llm_provider", "groq")
    monkeypatch.setattr(llm_module.settings, "llm_api_key", SecretStr(""))

    response = _ask(client, patient_auth_headers, "knee x-ray")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ASSISTANT_UNAVAILABLE"


class _UnreachableEmbeddings(EmbeddingProvider):
    """A provider whose every call fails as a timeout would."""

    def __init__(self) -> None:
        super().__init__(model="unreachable", dimensions=384)

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise TransientEmbeddingError("Hugging Face unreachable (ConnectTimeout)")


def test_ask_is_503_and_records_a_failure_when_the_question_cannot_be_embedded(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    llm = use_llm(FakeLLM("navigation"))
    app.dependency_overrides[get_query_embeddings] = lambda: _UnreachableEmbeddings()

    response = _ask(client, patient_auth_headers, "knee x-ray")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SEARCH_UNAVAILABLE"
    assert llm.calls == []
    assert _interaction(db_session).outcome == "FAILED"


# --- not blocking booking ----------------------------------------------------


class _TransactionSpy(FakeLLM):
    """A FakeLLM that notes, on each call, whether the request's database
    session had a transaction open."""

    def __init__(self, db: Session, *replies: str) -> None:
        super().__init__(*replies)
        self.db = db
        self.open: list[bool] = []

    async def complete(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> LLMResult:
        self.open.append(self.db.in_transaction())
        return await super().complete(messages, max_tokens=max_tokens)


def test_no_database_transaction_is_held_open_while_the_model_works(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """An open transaction keeps a pooled connection checked out. Held for
    the seconds a model takes, enough waiting questions would leave none
    for a booking."""
    spy = use_llm(_TransactionSpy(db_session, "navigation", "Knee X-Ray fits."))

    _ask(client, patient_auth_headers, "knee x-ray")

    assert spy.open == [False, False]


# --- PHI ---------------------------------------------------------------------


def test_ask_never_logs_the_question(
    client: TestClient,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The question is the patient's own words and can name a person or a
    condition. The logs carry ids only."""
    use_llm(FakeLLM("navigation", "Knee X-Ray fits."))
    caplog.set_level(logging.INFO, logger="app")

    _ask(client, patient_auth_headers, "knee x-ray for Zebulon")

    assert f"services=[{knee.id}]" in caplog.text
    assert "Zebulon" not in caplog.text

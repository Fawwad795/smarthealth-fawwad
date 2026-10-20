"""POST /assistant/ask and /assistant/ask/stream through the real FastAPI
routing layer (tasks 5.3 and 5.8).

The model is a FakeLLM scripted per test through app.dependency_overrides:
its first reply is the safety label, its second the answer. Retrieval runs
for real against services indexed with FakeEmbeddings, which ranks by
shared words, so "knee x-ray" finds the Knee X-Ray service and "hair
transplant" finds nothing. The threshold is fixed at 0.3, as in the search
tests.
"""

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta

import anyio
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
    LLMChunk,
    LLMMessage,
    LLMResult,
    TransientLLMError,
)
from app.ai.prompts import DISCLAIMER, NO_MATCH_V1, PROMPT_NAV_V1, PROMPT_PREP_V2
from app.ai.retrieval import get_query_embeddings
from app.core.config import settings
from app.main import app
from app.models import (
    AIInteraction,
    Appointment,
    Department,
    Patient,
    Provider,
    ProviderService,
    Service,
    Slot,
    User,
)
from app.models.enums import AppointmentStatus, ServiceStatus, SlotStatus, UserRole
from app.services import assistant as assistant_service
from app.services.assistant import (
    UNAVAILABLE_MESSAGE,
    AssistantAnswer,
    get_assistant_llm,
)
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
    assert row.retrieved_ids == {
        "services": [knee.id],
        "appointments": [],
        "slots": [],
    }
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
    assert row.retrieved_ids == {"services": [], "appointments": [], "slots": []}


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


# --- preparation and availability (task 5.5) ---------------------------------
#
# These questions are answered from the caller's own upcoming appointments
# and the services found, each with its next open times. Times are fixed and
# far ahead, so the formatted dates never depend on today: 04:00 UTC is 09:00
# in the clinic fixture's Asia/Karachi.

MONDAY_9AM = datetime(2030, 1, 7, 4, 0, tzinfo=UTC)


@pytest.fixture()
def other_patient(db_session: Session) -> Patient:
    """A second synthetic patient, whose appointments the caller must never
    see."""
    user = User(
        email="other.patient@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(user)
    db_session.flush()
    other = Patient(user_id=user.id, dob=date(1985, 5, 5))
    db_session.add(other)
    db_session.flush()
    return other


def _slot(
    db: Session,
    provider: Provider,
    starts_at: datetime,
    status: SlotStatus = SlotStatus.AVAILABLE,
) -> Slot:
    """A 30-minute slot of `provider`'s time."""
    slot = Slot(
        provider_id=provider.id,
        start_time=starts_at,
        end_time=starts_at + timedelta(minutes=30),
        status=status,
    )
    db.add(slot)
    db.flush()
    return slot


def _book(
    db: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    starts_at: datetime,
    status: AppointmentStatus = AppointmentStatus.CONFIRMED,
) -> Appointment:
    """An appointment on a new, booked slot, as the saga leaves one."""
    slot = _slot(db, provider, starts_at, SlotStatus.BOOKED)
    appointment = Appointment(
        patient_id=patient.id,
        provider_id=provider.id,
        slot_id=slot.id,
        service_id=service.id,
        status=status,
        idempotency_key=f"assistant-test-{slot.id}",
    )
    db.add(appointment)
    db.flush()
    return appointment


def _appointments_block(user_message: str) -> str:
    """The <appointments> block of a preparation prompt's user message."""
    return user_message.split("</appointments>")[0]


def test_a_preparation_question_is_answered_from_the_callers_own_appointments(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    other_patient: Patient,
    provider: Provider,
    knee: Service,
    service: Service,
    department: Department,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    """The other patient's Echocardiogram appears nowhere in what the model
    is sent."""
    own = _book(db_session, patient, provider, knee, MONDAY_9AM)
    _book(db_session, other_patient, provider, service, MONDAY_9AM + timedelta(hours=1))
    llm = use_llm(FakeLLM("preparation", "Wear loose clothing to your Knee X-Ray."))

    body = _ask(client, patient_auth_headers, "knee x-ray").json()

    assert body["outcome"] == "ANSWERED"
    assert body["intent"] == "PREPARATION"
    _, (system, user) = llm.calls
    assert system.content == PROMPT_PREP_V2.text.format(
        clinic=settings.assistant_clinic_name
    )
    assert _appointments_block(user.content) == (
        "<appointments>\n"
        f"- Knee X-Ray ({department.name})\n"
        "  When: Monday 07 January 2030, 09:00\n"
        "  Status: CONFIRMED\n"
        "  Preparation: Wear loose clothing.\n"
    )
    assert "Echocardiogram" not in user.content
    row = _interaction(db_session)
    assert row.prompt_version == "PROMPT_PREP_V2"
    assert row.retrieved_ids["appointments"] == [own.id]


def test_another_patients_appointment_for_the_same_service_is_never_shown(
    client: TestClient,
    db_session: Session,
    other_patient: Patient,
    provider: Provider,
    knee: Service,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    """The caller has no appointments. The other patient's Knee X-Ray
    matches the question exactly, and is still not shown: the query is
    scoped to the caller by the token, whatever the question says."""
    _book(db_session, other_patient, provider, knee, MONDAY_9AM)
    llm = use_llm(FakeLLM("availability", "You have no upcoming appointments."))

    _ask(client, patient_auth_headers, "when is my knee x-ray")

    user = llm.calls[1][1].content
    assert _appointments_block(user) == "<appointments>\n(none)\n"
    assert "CONFIRMED" not in user
    assert _interaction(db_session).retrieved_ids["appointments"] == []


def test_past_cancelled_and_finished_appointments_are_left_out(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    knee: Service,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    yesterday = datetime.now(UTC) - timedelta(days=1)
    _book(db_session, patient, provider, knee, yesterday)
    for hours, status in [
        (1, AppointmentStatus.CANCELLED),
        (2, AppointmentStatus.REJECTED),
        (3, AppointmentStatus.COMPLETED),
    ]:
        _book(
            db_session,
            patient,
            provider,
            knee,
            MONDAY_9AM + timedelta(hours=hours),
            status,
        )
    upcoming = _book(
        db_session, patient, provider, knee, MONDAY_9AM, AppointmentStatus.REQUESTED
    )
    llm = use_llm(FakeLLM("preparation", "Wear loose clothing."))

    _ask(client, patient_auth_headers, "knee x-ray")

    block = _appointments_block(llm.calls[1][1].content)
    assert block.count("- Knee X-Ray") == 1
    assert "Status: REQUESTED" in block
    assert _interaction(db_session).retrieved_ids["appointments"] == [upcoming.id]


def test_an_availability_question_shows_each_services_next_three_open_times(
    client: TestClient,
    db_session: Session,
    provider: Provider,
    knee: Service,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    """Only free, future slots of a provider who offers the service, in the
    clinic's timezone, earliest first."""
    db_session.add(ProviderService(provider_id=provider.id, service_id=knee.id))
    _slot(db_session, provider, MONDAY_9AM - timedelta(minutes=30), SlotStatus.RESERVED)
    _slot(db_session, provider, datetime.now(UTC) - timedelta(days=1))
    shown = [
        _slot(db_session, provider, MONDAY_9AM + timedelta(minutes=30 * n))
        for n in range(3)
    ]
    _slot(db_session, provider, MONDAY_9AM + timedelta(minutes=90))
    llm = use_llm(FakeLLM("availability", "The next open time is 09:00."))

    _ask(client, patient_auth_headers, "when can I book a knee x-ray")

    assert (
        "  Open times: Monday 07 January 2030, 09:00; Monday 07 January 2030, 09:30; "
        "Monday 07 January 2030, 10:00\n</services>"
    ) in llm.calls[1][1].content
    assert _interaction(db_session).retrieved_ids["slots"] == [s.id for s in shown]


def test_a_slot_shared_by_two_services_is_recorded_once(
    client: TestClient,
    db_session: Session,
    department: Department,
    provider: Provider,
    knee: Service,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    """A slot is the provider's time, so it is open for every service that
    provider offers. Both services list it; the row names it once. Found
    live: a dermatologist's slots were recorded once per dermatology
    service."""
    mri = index_service(
        db_session,
        department,
        "Knee MRI Scan",
        "Orthopaedics: Knee MRI Scan. Imaging of the knee joint.",
    )
    for service in (knee, mri):
        db_session.add(ProviderService(provider_id=provider.id, service_id=service.id))
    slots = [
        _slot(db_session, provider, MONDAY_9AM + timedelta(minutes=30 * n))
        for n in range(3)
    ]
    llm = use_llm(FakeLLM("availability", "The next open time is 09:00."))

    _ask(client, patient_auth_headers, "imaging of the knee joint")

    services_block = llm.calls[1][1].content.split("<services>")[1]
    assert services_block.count("Open times: Monday 07 January 2030, 09:00;") == 2
    assert _interaction(db_session).retrieved_ids["slots"] == [s.id for s in slots]


def test_a_navigation_question_never_loads_appointments(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    knee: Service,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    """The patient's own data is read only for a question about their
    bookings."""
    _book(db_session, patient, provider, knee, MONDAY_9AM)
    llm = use_llm(FakeLLM("navigation", "Knee X-Ray fits."))

    _ask(client, patient_auth_headers, "knee x-ray")

    assert "<appointments>" not in llm.calls[1][1].content
    assert _interaction(db_session).retrieved_ids["appointments"] == []


def test_a_booking_question_with_nothing_found_still_gets_an_answer(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    """A patient account with no patient record and nothing retrieved:
    the model is told so, rather than the patient being told the clinic
    does not offer "my appointment"."""
    llm = use_llm(FakeLLM("availability", "You have no upcoming appointments."))

    body = _ask(client, patient_auth_headers, "hair transplant").json()

    assert body["outcome"] == "ANSWERED"
    assert llm.calls[1][1].content.startswith(
        "<appointments>\n(none)\n</appointments>\n\n<services>\n(none)\n</services>"
    )


def test_no_transaction_is_held_open_while_the_model_answers_a_booking_question(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    knee: Service,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    """The appointments and open times are read after the safety check, so
    that second read must end its transaction too."""
    _book(db_session, patient, provider, knee, MONDAY_9AM)
    spy = use_llm(_TransactionSpy(db_session, "preparation", "Wear loose clothing."))

    _ask(client, patient_auth_headers, "knee x-ray")

    assert spy.open == [False, False]


# --- streaming (task 5.8) ----------------------------------------------------
#
# TestClient reads the whole stream before returning it, so these tests check
# the events and their order, not their timing. The disconnect tests call
# ask_stream() directly: TestClient cannot hang up part-way.

STREAM_URL = "/api/v1/assistant/ask/stream"


def _stream(client: TestClient, headers: dict, question: str) -> list[tuple[str, dict]]:
    """POST /assistant/ask/stream and return its events in order, as
    (name, data) pairs."""
    response = client.post(STREAM_URL, headers=headers, json={"question": question})
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    events = []
    for block in response.text.removesuffix("\n\n").split("\n\n"):
        name, data = block.split("\n")
        events.append(
            (name.removeprefix("event: "), json.loads(data.removeprefix("data: ")))
        )
    return events


def _streamed_text(events: list[tuple[str, dict]]) -> str:
    """The reply: every token event's text, joined in order."""
    return "".join(data["text"] for name, data in events if name == "token")


class _BreaksMidAnswer(FakeLLM):
    """Answers the safety check, then streams one piece of the answer and
    fails, as a dropped connection would."""

    async def stream(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> AsyncIterator[LLMChunk]:
        self.calls.append(list(messages))
        yield LLMChunk(text="Knee ")
        raise TransientLLMError("LLM provider unreachable (ReadError)")


class _StallsMidAnswer(FakeLLM):
    """Answers the safety check, then streams one piece of the answer and
    goes quiet, as a slow provider would. Notes when its stream is closed."""

    def __init__(self, *replies: str) -> None:
        super().__init__(*replies)
        self.closed = False

    async def stream(
        self, messages: list[LLMMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> AsyncIterator[LLMChunk]:
        self.calls.append(list(messages))
        try:
            yield LLMChunk(text="Knee ")
            await anyio.sleep_forever()
        finally:
            self.closed = True


def test_the_stream_sends_the_answer_piece_by_piece_then_citations_then_done(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    department: Department,
    knee: Service,
) -> None:
    use_llm(FakeLLM("navigation", "Knee X-Ray fits. Wear loose clothing."))

    events = _stream(client, patient_auth_headers, "knee x-ray")

    assert [name for name, _ in events] == ["token"] * 7 + ["citations", "done"]
    # One event per piece the provider streamed (FakeLLM streams a word at
    # a time), then the disclaimer.
    assert [data["text"] for _, data in events[:7]] == [
        "Knee ",
        "X-Ray ",
        "fits. ",
        "Wear ",
        "loose ",
        "clothing.",
        f"\n\n{DISCLAIMER}",
    ]
    assert events[7][1] == {
        "citations": [
            {
                "service_id": knee.id,
                "name": "Knee X-Ray",
                "department": department.name,
                "similarity": 0.696,
            }
        ]
    }
    assert events[8][1] == {
        "intent": "NAVIGATION",
        "outcome": "ANSWERED",
        "interaction_id": _interaction(db_session).id,
    }


def test_the_streamed_reply_matches_the_one_ask_returns(
    client: TestClient,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    use_llm(FakeLLM("navigation", "Knee X-Ray fits.", "navigation", "Knee X-Ray fits."))

    streamed = _streamed_text(_stream(client, patient_auth_headers, "knee x-ray"))
    asked = _ask(client, patient_auth_headers, "knee x-ray").json()["answer"]

    assert streamed == asked


def test_the_stream_records_the_interaction_with_ids_tokens_and_length(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    llm = use_llm(FakeLLM("navigation", "Knee X-Ray fits. Wear loose clothing."))

    events = _stream(client, patient_auth_headers, "knee x-ray")

    row = _interaction(db_session)
    assert row.outcome == "ANSWERED"
    assert row.intent == "NAVIGATION"
    assert row.prompt_version == "PROMPT_NAV_V1"
    assert row.retrieved_ids["services"] == [knee.id]
    assert row.input_tokens == _words(llm.calls)
    assert row.output_tokens == 1 + 6
    assert row.answer_chars == len(_streamed_text(events))


def test_a_refusal_is_streamed_as_one_piece_with_the_disclaimer(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """Fixed text, not generated, so there is nothing to stream."""
    llm = use_llm(FakeLLM("medical_advice", "An answer that must never be asked for."))

    events = _stream(client, patient_auth_headers, "diagnose my knee x-ray")

    assert [name for name, _ in events] == ["token", "citations", "done"]
    assert events[0][1]["text"].startswith("I can't give medical advice")
    assert events[0][1]["text"].endswith(DISCLAIMER)
    assert [c["service_id"] for c in events[1][1]["citations"]] == [knee.id]
    assert events[2][1]["outcome"] == "REFUSED"
    assert len(llm.calls) == 1
    assert _interaction(db_session).outcome == "REFUSED"


def test_we_dont_offer_that_is_streamed_with_no_citations(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    use_llm(FakeLLM("navigation", "An answer that must never be asked for."))

    events = _stream(client, patient_auth_headers, "hair transplant")

    no_match = NO_MATCH_V1.text.format(clinic=settings.assistant_clinic_name)
    assert events == [
        ("token", {"text": f"{no_match}\n\n{DISCLAIMER}"}),
        ("citations", {"citations": []}),
        (
            "done",
            {
                "intent": "NAVIGATION",
                "outcome": "NOT_OFFERED",
                "interaction_id": _interaction(db_session).id,
            },
        ),
    ]


def test_a_provider_failure_mid_answer_ends_the_stream_with_an_error_event(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """The 200 and the first piece have already gone, so the failure can
    only be reported in the stream."""
    use_llm(_BreaksMidAnswer("navigation"))

    events = _stream(client, patient_auth_headers, "knee x-ray")

    row = _interaction(db_session)
    assert events == [
        ("token", {"text": "Knee "}),
        (
            "error",
            {
                "error": {
                    "code": "ASSISTANT_UNAVAILABLE",
                    "message": UNAVAILABLE_MESSAGE,
                }
            },
        ),
        (
            "done",
            {"intent": "NAVIGATION", "outcome": "FAILED", "interaction_id": row.id},
        ),
    ]
    assert row.outcome == "FAILED"
    assert row.answer_chars == len("Knee ")


def test_the_stream_is_a_json_503_when_the_safety_check_fails(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    knee: Service,
) -> None:
    """Nothing has been sent yet, so the failure is an ordinary error."""
    use_llm(FakeLLM(TransientLLMError("LLM provider returned HTTP 503")))

    response = client.post(
        STREAM_URL, headers=patient_auth_headers, json={"question": "knee x-ray"}
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ASSISTANT_UNAVAILABLE"
    assert _interaction(db_session).outcome == "FAILED"


def test_a_bad_question_to_the_stream_is_422_before_any_ai_work(
    client: TestClient,
    db_session: Session,
    patient_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    llm = use_llm(FakeLLM("navigation"))

    response = client.post(
        STREAM_URL, headers=patient_auth_headers, json={"question": "!!!!"}
    )

    assert response.status_code == 422
    assert llm.calls == []
    assert db_session.scalars(select(AIInteraction)).all() == []


@pytest.mark.parametrize(
    ("headers", "expected"),
    [(None, 401), ("front_desk_auth_headers", 403)],
    ids=["signed-out", "staff"],
)
def test_the_stream_is_for_signed_in_patients_only(
    client: TestClient,
    use_llm: Callable[[FakeLLM], FakeLLM],
    request: pytest.FixtureRequest,
    headers: str | None,
    expected: int,
) -> None:
    llm = use_llm(FakeLLM("navigation"))

    response = client.post(
        STREAM_URL,
        headers=request.getfixturevalue(headers) if headers else {},
        json={"question": "knee x-ray"},
    )

    assert response.status_code == expected
    assert llm.calls == []


async def _start_stream(
    db: Session, user: User, llm: FakeLLM
) -> AsyncGenerator[str | AssistantAnswer, None]:
    """ask_stream() for "knee x-ray", as the route calls it."""
    return await assistant_service.ask_stream(
        db, user_id=user.id, question="knee x-ray", llm=llm, embeddings=FakeEmbeddings()
    )


async def test_a_patient_who_leaves_mid_answer_is_recorded_as_interrupted(
    db_session: Session, patient_user: User, knee: Service
) -> None:
    """What SSEResponse does on a disconnect: close the stream while it is
    paused at a yield. The model's stream is closed too, so the provider
    stops writing."""
    llm = _StallsMidAnswer("navigation")
    stream = await _start_stream(db_session, patient_user, llm)

    assert await anext(stream) == "Knee "
    await stream.aclose()

    row = _interaction(db_session)
    assert row.outcome == "INTERRUPTED"
    assert row.answer_chars == len("Knee ")
    # The safety check's one word: a stream cut short reports no counts.
    assert row.output_tokens == 1
    assert llm.closed


async def test_an_interrupted_answer_is_recorded_though_the_request_is_cancelled(
    db_session: Session, patient_user: User, knee: Service
) -> None:
    """The other way a disconnect arrives: Starlette cancels the request
    while the stream waits on the provider. anyio then cancels every later
    await too, so without the shield the write recording the question would
    be cancelled as well."""
    stream = await _start_stream(
        db_session, patient_user, _StallsMidAnswer("navigation")
    )

    with anyio.CancelScope() as scope:
        async for _ in stream:
            scope.cancel()

    row = _interaction(db_session)
    assert row.outcome == "INTERRUPTED"
    assert row.answer_chars == len("Knee ")

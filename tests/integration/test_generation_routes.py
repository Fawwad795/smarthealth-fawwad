"""Staff generation through the real routes (tasks 5.9 and 5.10).

The model is a FakeLLM scripted per test. Its stream yields a reply one
word at a time, so a test can check the text arrives in pieces. The
appointment starts at a fixed time far ahead: 04:00 UTC is 09:00 in the
clinic fixture's Asia/Karachi.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import llm as llm_module
from app.ai.llm import FakeLLM
from app.main import app
from app.models import (
    AIInteraction,
    AnalyticsDaily,
    Appointment,
    Department,
    GeneratedContent,
    Patient,
    Provider,
    Service,
    Slot,
    User,
)
from app.models.enums import AppointmentStatus, GenerationKind, SlotStatus
from app.services import generation as generation_service
from app.services.generation import get_generation_llm
from tests.streaming import BreaksMidStream, StallsMidStream, read_events, streamed_text

MONDAY_9AM = datetime(2030, 1, 7, 4, 0, tzinfo=UTC)

REPORT_URL = "/api/v1/reports/generate/utilisation"
JANUARY = {"start_date": "2030-01-01", "end_date": "2030-01-31"}

GOOD_REPORT = (
    '{"summary": "The clinic was lightly used.", '
    '"observations": ["Cardiology booked half of its open time."]}'
)


@pytest.fixture()
def use_llm(client: TestClient) -> Callable[[FakeLLM], FakeLLM]:
    """Make the generation routes talk to the given FakeLLM, and return it.
    The client fixture clears the override when the test ends."""

    def _use(llm: FakeLLM) -> FakeLLM:
        app.dependency_overrides[get_generation_llm] = lambda: llm
        return llm

    return _use


def _slot(
    db: Session, provider: Provider, starts_at: datetime, status: SlotStatus
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


@pytest.fixture()
def booked(
    db_session: Session, patient: Patient, provider: Provider, service: Service
) -> Appointment:
    """A confirmed appointment on Monday 7 January 2030 at 09:00, clinic
    time, for a service with preparation notes."""
    service.prep_instructions = "Arrive 15 minutes early."
    slot = _slot(db_session, provider, MONDAY_9AM, SlotStatus.BOOKED)
    appointment = Appointment(
        patient_id=patient.id,
        provider_id=provider.id,
        slot_id=slot.id,
        service_id=service.id,
        status=AppointmentStatus.CONFIRMED,
        idempotency_key="generation-test",
    )
    db_session.add(appointment)
    db_session.flush()
    return appointment


def _generate(client: TestClient, headers: dict, appointment_id: int, kind: str):
    return client.post(
        f"/api/v1/appointments/{appointment_id}/generate/{kind}", headers=headers
    )


def _saved(db: Session) -> GeneratedContent:
    """The one generated_content row the request wrote."""
    return db.scalars(select(GeneratedContent)).one()


# --- summaries and follow-ups ------------------------------------------------


def test_a_summary_streams_its_text_then_citations_then_done(
    client: TestClient,
    db_session: Session,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    booked: Appointment,
) -> None:
    use_llm(FakeLLM("Your Echocardiogram is confirmed."))

    events = read_events(
        _generate(client, front_desk_auth_headers, booked.id, "summary")
    )

    saved = _saved(db_session)
    assert events == [
        ("token", {"text": "Your "}),
        ("token", {"text": "Echocardiogram "}),
        ("token", {"text": "is "}),
        ("token", {"text": "confirmed."}),
        ("citations", {"appointment_id": booked.id, "service_id": booked.service_id}),
        (
            "done",
            {
                "kind": "SUMMARY",
                "outcome": "ANSWERED",
                "generated_content_id": saved.id,
                "interaction_id": saved.interaction_id,
            },
        ),
    ]


def test_the_summary_prompt_holds_the_appointment_and_nothing_about_the_patient(
    client: TestClient,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    booked: Appointment,
    patient_user: User,
) -> None:
    llm = use_llm(FakeLLM("Your Echocardiogram is confirmed."))

    _generate(client, front_desk_auth_headers, booked.id, "summary")

    ((_, user),) = llm.calls
    assert user.content == (
        "<appointment>\n"
        "Service: Echocardiogram\n"
        "Department: Cardiology\n"
        "Specialty: Cardiology\n"
        "When: Monday 07 January 2030, 09:00\n"
        "Status: confirmed\n"
        "Preparation: Arrive 15 minutes early.\n"
        "</appointment>"
    )
    assert patient_user.email not in user.content
    assert "1990" not in user.content  # the patient's date of birth


def test_a_summary_is_saved_with_its_prompt_version_model_and_interaction(
    client: TestClient,
    db_session: Session,
    front_desk_user: User,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    booked: Appointment,
) -> None:
    use_llm(FakeLLM("Your Echocardiogram is confirmed."))

    events = read_events(
        _generate(client, front_desk_auth_headers, booked.id, "summary")
    )

    saved = _saved(db_session)
    assert saved.kind == GenerationKind.SUMMARY
    assert saved.appointment_id == booked.id
    assert saved.report_start is None and saved.report_end is None
    assert saved.content == {"text": streamed_text(events)}
    assert saved.model == "fake-llm-v1"
    assert saved.prompt_version == "PROMPT_SUMMARY_V1"
    assert saved.created_by == front_desk_user.id
    interaction = db_session.get(AIInteraction, saved.interaction_id)
    assert interaction.user_id == front_desk_user.id
    assert interaction.intent == "STAFF_GENERATION"
    assert interaction.outcome == "ANSWERED"
    assert interaction.retrieved_ids == {
        "services": [booked.service_id],
        "appointments": [booked.id],
        "slots": [],
    }
    assert interaction.answer_chars == len(streamed_text(events))


def test_a_followup_uses_its_own_prompt_and_the_status_in_words(
    client: TestClient,
    db_session: Session,
    admin_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    booked: Appointment,
) -> None:
    """A cancelled appointment is shown as "cancelled", never the enum."""
    booked.status = AppointmentStatus.CANCELLED
    llm = use_llm(FakeLLM("Your appointment did not go ahead."))

    events = read_events(_generate(client, admin_auth_headers, booked.id, "followup"))

    system, user = llm.calls[0]
    assert "draft a follow-up message" in system.content
    assert "Status: cancelled\n" in user.content
    assert events[-1][1]["kind"] == "FOLLOWUP"
    assert _saved(db_session).prompt_version == "PROMPT_FOLLOWUP_V1"


def test_an_unknown_appointment_is_404_before_any_ai_work(
    client: TestClient,
    db_session: Session,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    llm = use_llm(FakeLLM("Never written."))

    response = _generate(client, front_desk_auth_headers, 999999, "summary")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "APPOINTMENT_NOT_FOUND"
    assert llm.calls == []
    assert db_session.scalars(select(GeneratedContent)).all() == []


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (None, 401),
        ("patient_auth_headers", 403),
        ("provider_auth_headers", 403),
    ],
    ids=["signed-out", "patient", "provider"],
)
@pytest.mark.parametrize("kind", ["summary", "followup"])
def test_appointment_generation_is_for_front_desk_and_admin_only(
    client: TestClient,
    use_llm: Callable[[FakeLLM], FakeLLM],
    booked: Appointment,
    request: pytest.FixtureRequest,
    headers: str | None,
    expected: int,
    kind: str,
) -> None:
    llm = use_llm(FakeLLM("Never written."))

    response = _generate(
        client, request.getfixturevalue(headers) if headers else {}, booked.id, kind
    )

    assert response.status_code == expected
    assert llm.calls == []


def test_a_provider_failure_mid_text_ends_with_an_error_event_and_saves_the_text(
    client: TestClient,
    db_session: Session,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    booked: Appointment,
) -> None:
    use_llm(BreaksMidStream(piece="Your "))

    events = read_events(
        _generate(client, front_desk_auth_headers, booked.id, "summary")
    )

    saved = _saved(db_session)
    assert events[0] == ("token", {"text": "Your "})
    assert events[1] == (
        "error",
        {
            "error": {
                "code": "GENERATION_UNAVAILABLE",
                "message": generation_service.UNAVAILABLE_MESSAGE,
            }
        },
    )
    assert events[2][0] == "done" and events[2][1]["outcome"] == "FAILED"
    assert saved.content == {"text": "Your "}
    assert db_session.get(AIInteraction, saved.interaction_id).outcome == "FAILED"


def test_generation_is_503_when_no_model_is_configured(
    client: TestClient,
    front_desk_auth_headers: dict,
    booked: Appointment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real dependency, with Groq and no key."""
    monkeypatch.setattr(llm_module.settings, "llm_provider", "groq")
    monkeypatch.setattr(llm_module.settings, "llm_api_key", SecretStr(""))

    response = _generate(client, front_desk_auth_headers, booked.id, "summary")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "GENERATION_UNAVAILABLE"


async def test_a_staff_member_who_leaves_mid_generation_still_has_the_text_saved(
    db_session: Session, front_desk_user: User, booked: Appointment
) -> None:
    """The brief: handle a disconnect "and still persist what was
    generated". SSEResponse closes the stream on a disconnect; this does
    the same."""
    llm = StallsMidStream(piece="Your ")
    stream = await generation_service.generate_for_appointment(
        db_session,
        user_id=front_desk_user.id,
        appointment_id=booked.id,
        kind=GenerationKind.SUMMARY,
        llm=llm,
    )

    assert await anext(stream) == "Your "
    await stream.aclose()

    saved = _saved(db_session)
    assert saved.content == {"text": "Your "}
    assert db_session.get(AIInteraction, saved.interaction_id).outcome == "INTERRUPTED"
    assert llm.closed


# --- utilisation reports -----------------------------------------------------


@pytest.fixture()
def january(
    db_session: Session, clinic, department: Department, provider: Provider
) -> Department:
    """Analytics and slots for January 2030, with a row and a slot just
    outside it, and a second department with no providers. Returns that
    second department."""
    db_session.add_all(
        [
            AnalyticsDaily(
                date=date(2030, 1, 7),
                appointments_booked=4,
                completed_visits=2,
                cancellations=1,
                wait_seconds_total=600.0,
                wait_count=2,
            ),
            AnalyticsDaily(date=date(2030, 2, 1), appointments_booked=9),
        ]
    )
    _slot(db_session, provider, MONDAY_9AM, SlotStatus.BOOKED)
    _slot(db_session, provider, MONDAY_9AM + timedelta(hours=1), SlotStatus.AVAILABLE)
    _slot(db_session, provider, MONDAY_9AM + timedelta(hours=2), SlotStatus.BLOCKED)
    _slot(
        db_session, provider, datetime(2030, 2, 1, 4, 0, tzinfo=UTC), SlotStatus.BOOKED
    )
    empty = Department(clinic_id=clinic.id, name="Dermatology", order_index=2)
    db_session.add(empty)
    db_session.flush()
    return empty


def test_the_report_figures_match_the_analytics_endpoint_and_count_each_departments_slots(
    client: TestClient,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    department: Department,
    january: Department,
) -> None:
    """The DoD: "its numbers match analytics". The analytics half is
    compared with GET /analytics/summary for the same range."""
    use_llm(FakeLLM(GOOD_REPORT))

    events = read_events(
        client.post(REPORT_URL, headers=front_desk_auth_headers, json=JANUARY)
    )

    report = dict(events)["report"]
    analytics = client.get(
        "/api/v1/analytics/summary", headers=front_desk_auth_headers, params=JANUARY
    ).json()
    assert report["figures"]["analytics"] == analytics
    assert analytics["appointments_booked"] == 4  # February's 9 left out
    # One booked and one open slot in January; the BLOCKED slot and
    # February's slot are left out.
    assert report["figures"]["departments"] == [
        {
            "department_id": department.id,
            "department": "Cardiology",
            "slots": 2,
            "booked_slots": 1,
            "utilisation": 0.5,
        },
        {
            "department_id": january.id,
            "department": "Dermatology",
            "slots": 0,
            "booked_slots": 0,
            "utilisation": None,
        },
    ]


def test_the_report_streams_the_models_json_then_the_validated_report(
    client: TestClient,
    db_session: Session,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    january: Department,
) -> None:
    llm = use_llm(FakeLLM(GOOD_REPORT))

    events = read_events(
        client.post(REPORT_URL, headers=front_desk_auth_headers, json=JANUARY)
    )

    names = [name for name, _ in events]
    assert names[-2:] == ["report", "done"]
    assert set(names[:-2]) == {"token"}
    assert streamed_text(events) == GOOD_REPORT
    report = events[-2][1]
    assert report["summary"] == "The clinic was lightly used."
    assert report["observations"] == ["Cardiology booked half of its open time."]
    # The model was shown the figures, as JSON between tags.
    user = llm.calls[0][1].content
    assert user.startswith("<figures>\n{") and '"appointments_booked": 4' in user
    saved = _saved(db_session)
    assert saved.kind == GenerationKind.UTILISATION_REPORT
    assert saved.appointment_id is None
    assert (saved.report_start, saved.report_end) == (
        date(2030, 1, 1),
        date(2030, 1, 31),
    )
    assert saved.content == report
    assert saved.prompt_version == "PROMPT_REPORT_V1"


def test_an_unusable_report_reply_is_retried_once_with_a_repair_instruction(
    client: TestClient,
    db_session: Session,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    january: Department,
) -> None:
    llm = use_llm(FakeLLM("Here is your report.", GOOD_REPORT))

    events = read_events(
        client.post(REPORT_URL, headers=front_desk_auth_headers, json=JANUARY)
    )

    names = [name for name, _ in events]
    retry = names.index("retry")
    assert "reply: Invalid JSON" in events[retry][1]["reason"]
    assert streamed_text(events[:retry]) == "Here is your report."
    assert streamed_text(events[retry:]) == GOOD_REPORT
    assert names[-2:] == ["report", "done"] and events[-1][1]["outcome"] == "ANSWERED"
    # The second call shows the model its own reply and what was wrong.
    first, second = llm.calls
    assert second[:2] == first
    assert (second[2].role, second[2].content) == ("assistant", "Here is your report.")
    assert second[3].content.startswith(
        "Your reply could not be used: reply: Invalid JSON"
    )
    assert _saved(db_session).prompt_version == "REPORT_REPAIR_V1"


def test_a_report_reply_with_a_digit_is_retried(
    client: TestClient,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    january: Department,
) -> None:
    """A digit would be a number the model wrote itself."""
    use_llm(
        FakeLLM(
            '{"summary": "Bookings reached 12.", "observations": ["Fine."]}',
            GOOD_REPORT,
        )
    )

    events = read_events(
        client.post(REPORT_URL, headers=front_desk_auth_headers, json=JANUARY)
    )

    assert "summary: Value error, contains a digit" in dict(events)["retry"]["reason"]
    assert dict(events)["report"]["summary"] == "The clinic was lightly used."


def test_two_unusable_report_replies_fail_cleanly(
    client: TestClient,
    db_session: Session,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    january: Department,
) -> None:
    """One retry only, then an error event and a saved FAILED run: no
    report, and no 500."""
    llm = use_llm(FakeLLM("Not JSON.", "Still not JSON."))

    events = read_events(
        client.post(REPORT_URL, headers=front_desk_auth_headers, json=JANUARY)
    )

    names = [name for name, _ in events]
    assert names.count("retry") == 1
    assert "report" not in names
    assert dict(events)["error"] == {
        "error": {
            "code": "REPORT_INVALID",
            "message": generation_service.REPORT_INVALID_MESSAGE,
        }
    }
    assert events[-1][1]["outcome"] == "FAILED"
    assert len(llm.calls) == 2
    saved = _saved(db_session)
    assert saved.content == {"text": "Not JSON.Still not JSON."}
    assert db_session.get(AIInteraction, saved.interaction_id).outcome == "FAILED"


def test_a_report_range_the_analytics_reject_is_422_before_any_ai_work(
    client: TestClient,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
) -> None:
    llm = use_llm(FakeLLM(GOOD_REPORT))

    response = client.post(
        REPORT_URL,
        headers=front_desk_auth_headers,
        json={"start_date": "2030-02-01", "end_date": "2030-01-01"},
    )

    assert response.status_code == 422
    assert llm.calls == []


def test_a_report_defaults_to_the_last_thirty_days(
    client: TestClient,
    db_session: Session,
    front_desk_auth_headers: dict,
    use_llm: Callable[[FakeLLM], FakeLLM],
    department: Department,
) -> None:
    use_llm(FakeLLM(GOOD_REPORT))

    read_events(client.post(REPORT_URL, headers=front_desk_auth_headers, json={}))

    today = datetime.now(UTC).date()
    saved = _saved(db_session)
    assert (saved.report_start, saved.report_end) == (today - timedelta(days=29), today)


@pytest.mark.parametrize("headers", ["patient_auth_headers", "provider_auth_headers"])
def test_reports_are_for_front_desk_and_admin_only(
    client: TestClient,
    use_llm: Callable[[FakeLLM], FakeLLM],
    request: pytest.FixtureRequest,
    headers: str,
) -> None:
    llm = use_llm(FakeLLM(GOOD_REPORT))

    response = client.post(
        REPORT_URL, headers=request.getfixturevalue(headers), json={}
    )

    assert response.status_code == 403
    assert llm.calls == []

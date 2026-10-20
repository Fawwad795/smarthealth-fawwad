"""GET /analytics/ai (task 5.11): AI usage counted from ai_interactions.

Every interaction is written with a fixed created_at on one past day, so the
numbers never depend on today or on now() inside the test's transaction,
which is the same for every row.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    AIInteraction,
    Appointment,
    Patient,
    Provider,
    Service,
    Slot,
    User,
)
from app.models.enums import (
    AppointmentStatus,
    AssistantIntent,
    InteractionOutcome,
    SlotStatus,
    UserRole,
)

AI = "/api/v1/analytics/ai"
DAY = date(2026, 3, 2)
NOON = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
ONE_DAY = {"start_date": DAY.isoformat(), "end_date": DAY.isoformat()}


def _interaction(
    db: Session,
    user: User,
    when: datetime,
    intent: AssistantIntent | None = AssistantIntent.NAVIGATION,
    outcome: InteractionOutcome = InteractionOutcome.ANSWERED,
    latency_ms: int = 100,
    tokens: tuple[int, int] = (0, 0),
) -> AIInteraction:
    """One ai_interactions row, written at `when`."""
    row = AIInteraction(
        user_id=user.id,
        intent=intent,
        outcome=outcome,
        latency_ms=latency_ms,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        created_at=when,
    )
    db.add(row)
    db.flush()
    return row


def _patient(db: Session, n: int) -> tuple[User, Patient]:
    """A synthetic patient and the user they sign in as."""
    user = User(
        email=f"ai.analytics.{n}@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db.add(user)
    db.flush()
    patient = Patient(user_id=user.id, dob=date(1990, 1, 1))
    db.add(patient)
    db.flush()
    return user, patient


def _booking(
    db: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    booked_at: datetime | None,
) -> Appointment:
    """An appointment on its own slot, booked (confirmed) at `booked_at`.

    None is an appointment the saga has not confirmed: it has no booked_at.
    """
    starts = datetime(2026, 3, 10, 4, 0, tzinfo=UTC) + timedelta(hours=patient.id)
    slot = Slot(
        provider_id=provider.id,
        start_time=starts,
        end_time=starts + timedelta(minutes=30),
        status=SlotStatus.BOOKED,
    )
    db.add(slot)
    db.flush()
    appointment = Appointment(
        patient_id=patient.id,
        provider_id=provider.id,
        slot_id=slot.id,
        service_id=service.id,
        status=(
            AppointmentStatus.CONFIRMED if booked_at else AppointmentStatus.REQUESTED
        ),
        idempotency_key=f"ai-analytics-{slot.id}",
        booked_at=booked_at,
    )
    db.add(appointment)
    db.flush()
    return appointment


def test_the_ai_analytics_count_outcomes_intents_latency_and_tokens(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    db_session: Session,
    patient_user: User,
    front_desk_user: User,
) -> None:
    """Questions and staff generations are counted apart; a refusal with no
    label is counted under "none"; the day's edges are respected."""
    for intent, outcome, latency, tokens in [
        (AssistantIntent.NAVIGATION, InteractionOutcome.ANSWERED, 100, (10, 2)),
        (AssistantIntent.NAVIGATION, InteractionOutcome.ANSWERED, 300, (20, 4)),
        (AssistantIntent.MEDICAL_ADVICE, InteractionOutcome.REFUSED, 200, (5, 1)),
        (None, InteractionOutcome.REFUSED, 50, (5, 0)),
        (AssistantIntent.PREPARATION, InteractionOutcome.FAILED, 400, (0, 0)),
    ]:
        _interaction(db_session, patient_user, NOON, intent, outcome, latency, tokens)
    _interaction(
        db_session,
        front_desk_user,
        NOON,
        AssistantIntent.STAFF_GENERATION,
        latency_ms=1000,
        tokens=(100, 50),
    )
    # Just outside the day, on either side.
    _interaction(db_session, patient_user, datetime(2026, 3, 1, 23, 59, 59, tzinfo=UTC))
    _interaction(db_session, patient_user, datetime(2026, 3, 3, 0, 0, tzinfo=UTC))

    response = client.get(AI, params=ONE_DAY, headers=admin_auth_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (
        body["interactions"],
        body["questions_asked"],
        body["staff_generations"],
    ) == (
        6,
        5,
        1,
    )
    assert body["by_outcome"] == {
        "ANSWERED": 3,
        "REFUSED": 2,
        "NOT_OFFERED": 0,
        "INTERRUPTED": 0,
        "FAILED": 1,
    }
    assert body["by_intent"] == {
        "MEDICAL_ADVICE": 1,
        "PREPARATION": 1,
        "AVAILABILITY": 0,
        "NAVIGATION": 2,
        "OTHER": 0,
        "STAFF_GENERATION": 1,
        "none": 1,
    }
    assert body["avg_latency_ms"] == pytest.approx(2050 / 6)
    assert (body["input_tokens"], body["output_tokens"], body["total_tokens"]) == (
        140,
        57,
        197,
    )


def test_the_p95_latency_interpolates_between_the_two_slowest(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    db_session: Session,
    patient_user: User,
) -> None:
    """Twenty latencies, 100 to 2000 ms: the 95th percentile sits 5% of the
    way from the 19th (1900) to the 20th (2000), as numpy computes it."""
    for latency in range(100, 2001, 100):
        _interaction(db_session, patient_user, NOON, latency_ms=latency)

    body = client.get(AI, params=ONE_DAY, headers=admin_auth_headers).json()

    assert body["p95_latency_ms"] == pytest.approx(1905.0)
    assert body["avg_latency_ms"] == pytest.approx(1050.0)


def test_a_range_with_no_interactions_has_zeros_and_no_rates(
    client: TestClient, front_desk_auth_headers: dict[str, str]
) -> None:
    """Every key is still there at 0; the averages and the rate are null,
    since there is nothing to divide."""
    response = client.get(AI, params=ONE_DAY, headers=front_desk_auth_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["interactions"] == 0
    assert set(body["by_outcome"].values()) == {0}
    assert len(body["by_outcome"]) == len(InteractionOutcome)
    assert set(body["by_intent"].values()) == {0}
    assert len(body["by_intent"]) == len(AssistantIntent) + 1
    assert body["avg_latency_ms"] is None
    assert body["p95_latency_ms"] is None
    assert body["booking_conversion"] == {
        "window_hours": 24,
        "patients_asked": 0,
        "patients_booked": 0,
        "rate": None,
    }


def test_booking_conversion_counts_patients_who_booked_within_a_day_of_asking(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    db_session: Session,
    provider: Provider,
    service: Service,
    front_desk_user: User,
) -> None:
    """Four patients asked; only the one who booked within 24 hours after a
    question converted.

    - booked 2 hours after asking, and asked twice: counted once, converted
    - booked 30 hours after asking: outside the window
    - booked an hour before asking: the question did not lead to it
    - never confirmed, so no booked_at; asked while the first patient
      booked, which must not count for them
    A staff generation is not a question, so its user is not counted.
    """
    converted_user, converted = _patient(db_session, 1)
    late_user, late = _patient(db_session, 2)
    earlier_user, earlier = _patient(db_session, 3)
    unconfirmed_user, unconfirmed = _patient(db_session, 4)

    _interaction(db_session, converted_user, NOON)
    _interaction(db_session, converted_user, NOON + timedelta(hours=1))
    _booking(db_session, converted, provider, service, NOON + timedelta(hours=2))

    _interaction(db_session, late_user, NOON)
    _booking(db_session, late, provider, service, NOON + timedelta(hours=30))

    _interaction(db_session, earlier_user, NOON)
    _booking(db_session, earlier, provider, service, NOON - timedelta(hours=1))

    _interaction(db_session, unconfirmed_user, NOON)
    _booking(db_session, unconfirmed, provider, service, None)

    _interaction(db_session, front_desk_user, NOON, AssistantIntent.STAFF_GENERATION)

    body = client.get(AI, params=ONE_DAY, headers=admin_auth_headers).json()

    assert body["booking_conversion"] == {
        "window_hours": 24,
        "patients_asked": 4,
        "patients_booked": 1,
        "rate": 0.25,
    }


def test_a_booking_exactly_a_day_after_the_question_still_counts(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    db_session: Session,
    provider: Provider,
    service: Service,
) -> None:
    """The window's end is inclusive: 24 hours to the second."""
    user, patient = _patient(db_session, 1)
    _interaction(db_session, user, NOON)
    _booking(db_session, patient, provider, service, NOON + timedelta(hours=24))

    body = client.get(AI, params=ONE_DAY, headers=admin_auth_headers).json()

    assert body["booking_conversion"]["patients_booked"] == 1

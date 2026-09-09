"""The visit lifecycle through the real FastAPI routing layer.

Covers task 2.12's "illegal transitions" alongside 2.11 itself: every
skip, every reversal, and every retry of an already-made transition.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models import (
    Appointment,
    AppointmentStatusHistory,
    Patient,
    Provider,
    Service,
    Slot,
    User,
    Visit,
)
from app.models.enums import AppointmentStatus, SlotStatus, UserRole


def _confirmed_appointment(
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> Appointment:
    """An appointment in the state a completed saga leaves behind."""
    slot.status = SlotStatus.BOOKED
    appointment = Appointment(
        patient_id=patient.id,
        provider_id=provider.id,
        slot_id=slot.id,
        service_id=service.id,
        status=AppointmentStatus.CONFIRMED,
        idempotency_key=f"key-visit-{slot.id}",
        booked_at=datetime.now(timezone.utc),
    )
    db_session.add(appointment)
    db_session.flush()
    db_session.add(
        AppointmentStatusHistory(
            appointment_id=appointment.id,
            from_status=None,
            to_status=AppointmentStatus.REQUESTED,
            actor="PATIENT",
        )
    )
    db_session.flush()
    return appointment


def _url(appointment_id: int, action: str = "") -> str:
    return f"/api/v1/appointments/{appointment_id}/visit{action}"


def test_check_in_creates_the_visit(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    front_desk_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)

    response = client.post(
        _url(appointment.id, "/check-in"), headers=front_desk_auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["appointment_id"] == appointment.id
    assert body["status"] == "CHECKED_IN"
    assert body["checked_in_at"] is not None
    assert body["completed_at"] is None


def test_check_in_is_idempotent(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    front_desk_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)

    first = client.post(
        _url(appointment.id, "/check-in"), headers=front_desk_auth_headers
    )
    second = client.post(
        _url(appointment.id, "/check-in"), headers=front_desk_auth_headers
    )

    assert first.json()["id"] == second.json()["id"]
    assert (
        db_session.query(Visit).filter(Visit.appointment_id == appointment.id).count()
        == 1
    )


def test_check_in_does_not_reset_an_advanced_visit(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    front_desk_auth_headers: dict,
) -> None:
    """ "Doesn't advance the visit twice" cuts both ways -- a retried
    check-in must not drag a started visit back to CHECKED_IN either.
    """
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)
    client.post(_url(appointment.id, "/check-in"), headers=front_desk_auth_headers)
    client.post(_url(appointment.id, "/start"), headers=front_desk_auth_headers)

    response = client.post(
        _url(appointment.id, "/check-in"), headers=front_desk_auth_headers
    )

    assert response.status_code == 200
    assert response.json()["status"] == "IN_PROGRESS"


def test_check_in_on_an_unconfirmed_appointment_returns_409(
    client: TestClient, appointment: Appointment, front_desk_auth_headers: dict
) -> None:
    # The default `appointment` fixture is REQUESTED, never confirmed.
    response = client.post(
        _url(appointment.id, "/check-in"), headers=front_desk_auth_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPOINTMENT_NOT_CONFIRMED"


def test_start_moves_the_visit_to_in_progress(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    provider_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)
    client.post(_url(appointment.id, "/check-in"), headers=provider_auth_headers)

    response = client.post(
        _url(appointment.id, "/start"), headers=provider_auth_headers
    )

    assert response.status_code == 200
    assert response.json()["status"] == "IN_PROGRESS"


def test_start_is_idempotent(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    provider_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)
    client.post(_url(appointment.id, "/check-in"), headers=provider_auth_headers)
    client.post(_url(appointment.id, "/start"), headers=provider_auth_headers)

    response = client.post(
        _url(appointment.id, "/start"), headers=provider_auth_headers
    )

    assert response.status_code == 200
    assert response.json()["status"] == "IN_PROGRESS"


def test_start_without_check_in_returns_404(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    provider_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)

    response = client.post(
        _url(appointment.id, "/start"), headers=provider_auth_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "VISIT_NOT_FOUND"


def test_complete_finishes_the_visit_and_the_appointment(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    provider_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)
    client.post(_url(appointment.id, "/check-in"), headers=provider_auth_headers)
    client.post(_url(appointment.id, "/start"), headers=provider_auth_headers)

    response = client.post(
        _url(appointment.id, "/complete"), headers=provider_auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["completed_at"] is not None

    db_session.refresh(appointment)
    assert appointment.status == AppointmentStatus.COMPLETED

    history = (
        db_session.query(AppointmentStatusHistory)
        .filter(
            AppointmentStatusHistory.appointment_id == appointment.id,
            AppointmentStatusHistory.to_status == AppointmentStatus.COMPLETED,
        )
        .one()
    )
    assert history.from_status == AppointmentStatus.CONFIRMED
    assert history.actor == "PROVIDER"


def test_complete_is_idempotent(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    provider_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)
    client.post(_url(appointment.id, "/check-in"), headers=provider_auth_headers)
    client.post(_url(appointment.id, "/start"), headers=provider_auth_headers)
    first = client.post(
        _url(appointment.id, "/complete"), headers=provider_auth_headers
    )

    second = client.post(
        _url(appointment.id, "/complete"), headers=provider_auth_headers
    )

    assert second.status_code == 200
    assert first.json()["completed_at"] == second.json()["completed_at"]
    assert (
        db_session.query(AppointmentStatusHistory)
        .filter(
            AppointmentStatusHistory.appointment_id == appointment.id,
            AppointmentStatusHistory.to_status == AppointmentStatus.COMPLETED,
        )
        .count()
        == 1
    )


def test_complete_without_starting_returns_409(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    provider_auth_headers: dict,
) -> None:
    """The brief's own named illegal jump: completing a visit that never
    started.
    """
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)
    client.post(_url(appointment.id, "/check-in"), headers=provider_auth_headers)

    response = client.post(
        _url(appointment.id, "/complete"), headers=provider_auth_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VISIT_NOT_IN_PROGRESS"


def test_complete_without_check_in_returns_404(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    provider_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)

    response = client.post(
        _url(appointment.id, "/complete"), headers=provider_auth_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "VISIT_NOT_FOUND"


def test_start_after_complete_returns_409(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    provider_auth_headers: dict,
) -> None:
    """A visit never moves backward."""
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)
    client.post(_url(appointment.id, "/check-in"), headers=provider_auth_headers)
    client.post(_url(appointment.id, "/start"), headers=provider_auth_headers)
    client.post(_url(appointment.id, "/complete"), headers=provider_auth_headers)

    response = client.post(
        _url(appointment.id, "/start"), headers=provider_auth_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VISIT_ALREADY_COMPLETED"


def test_patient_cannot_check_themselves_in(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    patient_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)

    response = client.post(
        _url(appointment.id, "/check-in"), headers=patient_auth_headers
    )

    assert response.status_code == 403


def test_patient_can_read_their_own_visit(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    front_desk_auth_headers: dict,
    patient_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)
    client.post(_url(appointment.id, "/check-in"), headers=front_desk_auth_headers)

    response = client.get(_url(appointment.id), headers=patient_auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "CHECKED_IN"


def test_a_different_patient_cannot_read_the_visit(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    front_desk_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)
    client.post(_url(appointment.id, "/check-in"), headers=front_desk_auth_headers)

    other_user = User(
        email="visit-other-patient@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(other_user)
    db_session.flush()
    other_headers = {"Authorization": f"Bearer {create_access_token(other_user.id)}"}

    response = client.get(_url(appointment.id), headers=other_headers)

    assert response.status_code == 403


def test_get_visit_before_check_in_returns_404(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    front_desk_auth_headers: dict,
) -> None:
    appointment = _confirmed_appointment(db_session, patient, provider, service, slot)

    response = client.get(_url(appointment.id), headers=front_desk_auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "VISIT_NOT_FOUND"

"""POST /appointments/{id}/cancel through the real FastAPI routing layer."""

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
    SlotReservation,
    User,
    Waitlist,
)
from app.models.enums import (
    AppointmentStatus,
    SlotReservationStatus,
    SlotStatus,
    UserRole,
    WaitlistStatus,
)


def _reserved_appointment(
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
    to_status: AppointmentStatus = AppointmentStatus.SLOT_RESERVED,
) -> Appointment:
    """Build an appointment past REQUESTED, with the slot and reservation
    row a real saga run would have left behind at that status.
    """
    slot.status = (
        SlotStatus.RESERVED
        if to_status == AppointmentStatus.SLOT_RESERVED
        else SlotStatus.BOOKED
    )
    appointment = Appointment(
        patient_id=patient.id,
        provider_id=provider.id,
        slot_id=slot.id,
        service_id=service.id,
        status=to_status,
        idempotency_key=f"key-cancel-{to_status.value.lower()}",
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
    db_session.add(
        SlotReservation(
            appointment_id=appointment.id,
            slot_id=slot.id,
            status=(
                SlotReservationStatus.COMMITTED
                if to_status == AppointmentStatus.CONFIRMED
                else SlotReservationStatus.RESERVED
            ),
        )
    )
    db_session.flush()
    return appointment


def test_cancel_a_requested_appointment_needs_no_slot_release(
    client: TestClient,
    db_session: Session,
    appointment: Appointment,
    patient_auth_headers: dict,
    slot: Slot,
) -> None:
    # The `appointment` fixture is REQUESTED with no reservation -- cancel
    # must not try to touch a SlotReservation row that was never created.
    response = client.post(
        f"/api/v1/appointments/{appointment.id}/cancel", headers=patient_auth_headers
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    db_session.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE


def test_cancel_releases_a_reserved_slot(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    appointment = _reserved_appointment(db_session, patient, provider, service, slot)

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/cancel", headers=patient_auth_headers
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    db_session.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE
    reservation = (
        db_session.query(SlotReservation)
        .filter(SlotReservation.appointment_id == appointment.id)
        .one()
    )
    assert reservation.status == SlotReservationStatus.RELEASED


def test_cancel_releases_a_confirmed_appointments_slot(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    appointment = _reserved_appointment(
        db_session, patient, provider, service, slot, AppointmentStatus.CONFIRMED
    )

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/cancel", headers=patient_auth_headers
    )

    assert response.status_code == 200
    db_session.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE


def test_cancel_promotes_the_oldest_waiting_entry(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    appointment = _reserved_appointment(db_session, patient, provider, service, slot)

    waiting_user = User(
        email="waiting-patient@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(waiting_user)
    db_session.flush()
    waiting_patient = Patient(user_id=waiting_user.id, dob=patient.dob)
    db_session.add(waiting_patient)
    db_session.flush()
    entry = Waitlist(provider_id=provider.id, patient_id=waiting_patient.id)
    db_session.add(entry)
    db_session.flush()

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/cancel", headers=patient_auth_headers
    )

    assert response.status_code == 200
    db_session.refresh(entry)
    assert entry.status == WaitlistStatus.OFFERED


def test_cancel_with_nobody_waiting_does_not_error(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    appointment = _reserved_appointment(db_session, patient, provider, service, slot)

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/cancel", headers=patient_auth_headers
    )

    assert response.status_code == 200


def test_cancelling_an_already_cancelled_appointment_returns_409(
    client: TestClient, appointment: Appointment, patient_auth_headers: dict
) -> None:
    first = client.post(
        f"/api/v1/appointments/{appointment.id}/cancel", headers=patient_auth_headers
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/appointments/{appointment.id}/cancel", headers=patient_auth_headers
    )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "APPOINTMENT_NOT_CANCELLABLE"


def test_patient_cannot_cancel_another_patients_appointment(
    client: TestClient, db_session: Session, appointment: Appointment
) -> None:
    other_user = User(
        email="cancel-other-patient@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(other_user)
    db_session.flush()
    other_headers = {"Authorization": f"Bearer {create_access_token(other_user.id)}"}

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/cancel", headers=other_headers
    )

    assert response.status_code == 403


def test_front_desk_can_cancel_any_patients_appointment(
    client: TestClient, appointment: Appointment, front_desk_auth_headers: dict
) -> None:
    response = client.post(
        f"/api/v1/appointments/{appointment.id}/cancel",
        headers=front_desk_auth_headers,
    )

    assert response.status_code == 200


def test_cancel_unknown_appointment_returns_404(
    client: TestClient, patient_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/appointments/999999/cancel", headers=patient_auth_headers
    )

    assert response.status_code == 404

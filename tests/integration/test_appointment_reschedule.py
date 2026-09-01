"""POST /appointments/{id}/reschedule through the real FastAPI routing layer."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
import pytest

from app.core.exceptions import AppError
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
from app.services import appointment_scheduling


def _make_slot(db_session: Session, provider: Provider, hours_offset: int) -> Slot:
    start = datetime.now(timezone.utc) + timedelta(days=1, hours=hours_offset)
    s = Slot(
        provider_id=provider.id,
        start_time=start,
        end_time=start + timedelta(minutes=30),
    )
    db_session.add(s)
    db_session.flush()
    return s


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
        idempotency_key=f"key-reschedule-{to_status.value.lower()}-{slot.id}",
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


def test_reschedule_a_slot_reserved_appointment_moves_the_reservation(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    appointment = _reserved_appointment(db_session, patient, provider, service, slot)
    new_slot = _make_slot(db_session, provider, hours_offset=5)

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/reschedule",
        json={"new_slot_id": new_slot.id},
        headers=patient_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["slot_id"] == new_slot.id
    assert body["status"] == "SLOT_RESERVED"  # unchanged

    db_session.refresh(slot)
    db_session.refresh(new_slot)
    assert slot.status == SlotStatus.AVAILABLE
    assert new_slot.status == SlotStatus.RESERVED

    old_res = (
        db_session.query(SlotReservation)
        .filter(
            SlotReservation.appointment_id == appointment.id,
            SlotReservation.slot_id == slot.id,
        )
        .one()
    )
    new_res = (
        db_session.query(SlotReservation)
        .filter(
            SlotReservation.appointment_id == appointment.id,
            SlotReservation.slot_id == new_slot.id,
        )
        .one()
    )
    assert old_res.status == SlotReservationStatus.RELEASED
    assert new_res.status == SlotReservationStatus.RESERVED


def test_reschedule_a_confirmed_appointment_books_the_new_slot(
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
    new_slot = _make_slot(db_session, provider, hours_offset=5)

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/reschedule",
        json={"new_slot_id": new_slot.id},
        headers=patient_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CONFIRMED"  # unchanged, no re-billing

    db_session.refresh(new_slot)
    assert new_slot.status == SlotStatus.BOOKED  # not just RESERVED

    new_res = (
        db_session.query(SlotReservation)
        .filter(
            SlotReservation.appointment_id == appointment.id,
            SlotReservation.slot_id == new_slot.id,
        )
        .one()
    )
    assert new_res.status == SlotReservationStatus.COMMITTED


def test_reschedule_promotes_the_oldest_waiting_entry(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    appointment = _reserved_appointment(db_session, patient, provider, service, slot)
    new_slot = _make_slot(db_session, provider, hours_offset=5)

    waiting_user = User(
        email="reschedule-waiting@example.com",
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
        f"/api/v1/appointments/{appointment.id}/reschedule",
        json={"new_slot_id": new_slot.id},
        headers=patient_auth_headers,
    )

    assert response.status_code == 200
    db_session.refresh(entry)
    assert entry.status == WaitlistStatus.OFFERED


def test_reschedule_to_an_unavailable_slot_returns_409(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    appointment = _reserved_appointment(db_session, patient, provider, service, slot)
    taken_slot = _make_slot(db_session, provider, hours_offset=5)
    taken_slot.status = SlotStatus.BOOKED
    db_session.flush()

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/reschedule",
        json={"new_slot_id": taken_slot.id},
        headers=patient_auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SLOT_UNAVAILABLE"


def test_a_failed_reschedule_leaves_the_original_slot_untouched(
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    """The atomicity proof, at the service layer rather than through
    TestClient. The route test above cannot observe this: the test
    client's shared db_session has no per-request rollback boundary the
    way a real request's get_db() gets from Session.close(), so a
    mutation made before the eventual failure would wrongly appear to
    have "stuck." Committing the fixture setup, then rolling back after
    the expected exception, reproduces that exact boundary -- confirmed
    live against the real API first, where the same scenario correctly
    left the original slot BOOKED and the appointment pointing at it.
    """
    appointment = _reserved_appointment(db_session, patient, provider, service, slot)
    taken_slot = _make_slot(db_session, provider, hours_offset=5)
    taken_slot.status = SlotStatus.BOOKED
    db_session.commit()

    with pytest.raises(AppError) as exc_info:
        appointment_scheduling.reschedule_appointment(
            db_session, appointment.id, taken_slot.id
        )
    assert exc_info.value.code == "SLOT_UNAVAILABLE"
    db_session.rollback()

    db_session.refresh(appointment)
    db_session.refresh(slot)
    assert appointment.slot_id == slot.id
    assert slot.status == SlotStatus.RESERVED

    reservation = (
        db_session.query(SlotReservation)
        .filter(SlotReservation.appointment_id == appointment.id)
        .one()
    )
    assert reservation.status == SlotReservationStatus.RESERVED


def test_reschedule_to_a_different_providers_slot_returns_409(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    appointment = _reserved_appointment(db_session, patient, provider, service, slot)

    other_provider_user = User(
        email="reschedule-other-provider@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PROVIDER,
    )
    db_session.add(other_provider_user)
    db_session.flush()
    other_provider = Provider(
        user_id=other_provider_user.id,
        department_id=provider.department_id,
        specialty_id=provider.specialty_id,
    )
    db_session.add(other_provider)
    db_session.flush()
    other_slot = _make_slot(db_session, other_provider, hours_offset=5)

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/reschedule",
        json={"new_slot_id": other_slot.id},
        headers=patient_auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SLOT_WRONG_PROVIDER"


def test_reschedule_to_an_unknown_slot_returns_404(
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
        f"/api/v1/appointments/{appointment.id}/reschedule",
        json={"new_slot_id": 999999},
        headers=patient_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SLOT_NOT_FOUND"


def test_reschedule_a_requested_appointment_returns_409(
    client: TestClient,
    appointment: Appointment,
    patient_auth_headers: dict,
    slot: Slot,
) -> None:
    # The default `appointment` fixture is REQUESTED -- never held a
    # slot, so there is nothing to reschedule from.
    response = client.post(
        f"/api/v1/appointments/{appointment.id}/reschedule",
        json={"new_slot_id": slot.id},
        headers=patient_auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPOINTMENT_NOT_RESCHEDULABLE"


def test_patient_cannot_reschedule_another_patients_appointment(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    appointment = _reserved_appointment(db_session, patient, provider, service, slot)
    new_slot = _make_slot(db_session, provider, hours_offset=5)

    other_user = User(
        email="reschedule-other-patient@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(other_user)
    db_session.flush()
    other_headers = {"Authorization": f"Bearer {create_access_token(other_user.id)}"}

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/reschedule",
        json={"new_slot_id": new_slot.id},
        headers=other_headers,
    )

    assert response.status_code == 403


def test_front_desk_can_reschedule_any_patients_appointment(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    front_desk_auth_headers: dict,
    provider: Provider,
    service: Service,
    slot: Slot,
) -> None:
    appointment = _reserved_appointment(db_session, patient, provider, service, slot)
    new_slot = _make_slot(db_session, provider, hours_offset=5)

    response = client.post(
        f"/api/v1/appointments/{appointment.id}/reschedule",
        json={"new_slot_id": new_slot.id},
        headers=front_desk_auth_headers,
    )

    assert response.status_code == 200


def test_reschedule_unknown_appointment_returns_404(
    client: TestClient, patient_auth_headers: dict, slot: Slot
) -> None:
    response = client.post(
        "/api/v1/appointments/999999/reschedule",
        json={"new_slot_id": slot.id},
        headers=patient_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "APPOINTMENT_NOT_FOUND"

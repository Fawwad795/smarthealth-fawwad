"""Tests for SchedulingActivities, called directly as plain methods -- no
Temporal server involved. Same pattern as test_publish_activities.py:
constructed with a factory handing back this test's own db_session
(wrapped in nullcontext so the Activity's `with ... as db:` doesn't close
it), so every write lands in the same rolled-back transaction as the
rest of the suite.
"""

from contextlib import nullcontext

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from temporalio.exceptions import ApplicationError

from app.core.config import settings
from app.models import (
    Appointment,
    AppointmentStatusHistory,
    Notification,
    ProviderService,
    Service,
    Slot,
    SlotReservation,
)
from app.models.enums import (
    AppointmentStatus,
    NotificationType,
    ServiceStatus,
    SlotReservationStatus,
    SlotStatus,
)
from app.temporal.activities import RejectInput, ReleaseSlotInput, SchedulingActivities


@pytest.fixture()
def activities(db_session: Session) -> SchedulingActivities:
    return SchedulingActivities(session_factory=lambda: nullcontext(db_session))


def _history_count(db_session: Session, appointment_id: int) -> int:
    return db_session.execute(
        select(func.count())
        .select_from(AppointmentStatusHistory)
        .where(AppointmentStatusHistory.appointment_id == appointment_id)
    ).scalar_one()


# --- validate_eligibility ---------------------------------------------------


def test_validate_eligibility_passes_when_published_and_offered(
    activities: SchedulingActivities,
    db_session: Session,
    appointment: Appointment,
    service: Service,
    provider_service_link: ProviderService,
) -> None:
    service.status = ServiceStatus.PUBLISHED
    db_session.flush()

    activities.validate_eligibility(appointment.id)  # does not raise


def test_validate_eligibility_raises_listing_every_reason(
    activities: SchedulingActivities, appointment: Appointment
) -> None:
    # appointment's service defaults to DRAFT (not published) and has no
    # provider_service_link -- two independent reasons, both expected back.
    with pytest.raises(ApplicationError) as exc_info:
        activities.validate_eligibility(appointment.id)

    assert exc_info.value.type == "APPOINTMENT_INELIGIBLE"
    assert exc_info.value.non_retryable is True
    assert "not published" in str(exc_info.value)
    assert "does not offer" in str(exc_info.value)


# --- reserve_slot ------------------------------------------------------------


def test_reserve_slot_reserves_and_records_history(
    activities: SchedulingActivities,
    db_session: Session,
    appointment: Appointment,
    slot: Slot,
) -> None:
    activities.reserve_slot(appointment.id)

    db_session.refresh(slot)
    db_session.refresh(appointment)
    assert slot.status == SlotStatus.RESERVED
    assert appointment.status == AppointmentStatus.SLOT_RESERVED
    reservation = db_session.execute(
        select(SlotReservation).where(SlotReservation.appointment_id == appointment.id)
    ).scalar_one()
    assert reservation.status == SlotReservationStatus.RESERVED


def test_reserve_slot_is_idempotent(
    activities: SchedulingActivities, db_session: Session, appointment: Appointment
) -> None:
    activities.reserve_slot(appointment.id)

    activities.reserve_slot(appointment.id)  # simulates a Temporal retry

    reservations = (
        db_session.execute(
            select(SlotReservation).where(
                SlotReservation.appointment_id == appointment.id
            )
        )
        .scalars()
        .all()
    )
    assert len(reservations) == 1
    assert _history_count(db_session, appointment.id) == 2  # REQUESTED, SLOT_RESERVED


def test_reserve_slot_raises_when_slot_already_taken(
    activities: SchedulingActivities,
    db_session: Session,
    appointment: Appointment,
    slot: Slot,
) -> None:
    slot.status = SlotStatus.BOOKED  # someone else already holds it
    db_session.flush()

    with pytest.raises(ApplicationError) as exc_info:
        activities.reserve_slot(appointment.id)

    assert exc_info.value.type == "SLOT_UNAVAILABLE"
    assert exc_info.value.non_retryable is True


# --- billing_precheck --------------------------------------------------------


def test_billing_precheck_passes_by_default(
    activities: SchedulingActivities, appointment: Appointment
) -> None:
    activities.billing_precheck(appointment.id)  # does not raise


def test_billing_precheck_raises_when_forced(
    activities: SchedulingActivities, appointment: Appointment, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "billing_force_fail", True)

    with pytest.raises(ApplicationError) as exc_info:
        activities.billing_precheck(appointment.id)

    assert exc_info.value.type == "BILLING_FAILED"


# --- confirm ------------------------------------------------------------------


def test_confirm_completes_the_happy_path(
    activities: SchedulingActivities,
    db_session: Session,
    appointment: Appointment,
    slot: Slot,
) -> None:
    activities.reserve_slot(appointment.id)

    activities.confirm(appointment.id)

    db_session.refresh(appointment)
    db_session.refresh(slot)
    assert appointment.status == AppointmentStatus.CONFIRMED
    assert appointment.booked_at is not None
    assert slot.status == SlotStatus.BOOKED
    reservation = db_session.execute(
        select(SlotReservation).where(SlotReservation.appointment_id == appointment.id)
    ).scalar_one()
    assert reservation.status == SlotReservationStatus.COMMITTED


def test_confirm_is_idempotent(
    activities: SchedulingActivities, db_session: Session, appointment: Appointment
) -> None:
    activities.reserve_slot(appointment.id)
    activities.confirm(appointment.id)
    db_session.refresh(appointment)
    first_booked_at = appointment.booked_at

    activities.confirm(appointment.id)  # simulates a retry after a lost result

    db_session.refresh(appointment)
    assert appointment.booked_at == first_booked_at
    assert (
        _history_count(db_session, appointment.id) == 3
    )  # not a duplicate CONFIRMED row


# --- release_slot (compensation) ---------------------------------------------


def test_release_slot_compensates_correctly(
    activities: SchedulingActivities,
    db_session: Session,
    appointment: Appointment,
    slot: Slot,
) -> None:
    activities.reserve_slot(appointment.id)

    activities.release_slot(
        ReleaseSlotInput(appointment.id, "billing pre-check failed")
    )

    db_session.refresh(appointment)
    db_session.refresh(slot)
    assert appointment.status == AppointmentStatus.CANCELLED
    assert slot.status == SlotStatus.AVAILABLE
    reservation = db_session.execute(
        select(SlotReservation).where(SlotReservation.appointment_id == appointment.id)
    ).scalar_one()
    assert reservation.status == SlotReservationStatus.RELEASED
    history = db_session.execute(
        select(AppointmentStatusHistory).where(
            AppointmentStatusHistory.appointment_id == appointment.id,
            AppointmentStatusHistory.to_status == AppointmentStatus.CANCELLED,
        )
    ).scalar_one()
    assert history.actor == "SAGA_COMPENSATION"


def test_release_slot_is_idempotent(
    activities: SchedulingActivities, db_session: Session, appointment: Appointment
) -> None:
    activities.reserve_slot(appointment.id)
    activities.release_slot(
        ReleaseSlotInput(appointment.id, "billing pre-check failed")
    )

    activities.release_slot(
        ReleaseSlotInput(appointment.id, "billing pre-check failed")
    )

    assert (
        _history_count(db_session, appointment.id) == 3
    )  # not a duplicate CANCELLED row


# --- reject -------------------------------------------------------------------


def test_reject_transitions_and_records_reason(
    activities: SchedulingActivities, db_session: Session, appointment: Appointment
) -> None:
    activities.reject(RejectInput(appointment.id, "service is not published"))

    db_session.refresh(appointment)
    assert appointment.status == AppointmentStatus.REJECTED
    history = db_session.execute(
        select(AppointmentStatusHistory).where(
            AppointmentStatusHistory.appointment_id == appointment.id,
            AppointmentStatusHistory.to_status == AppointmentStatus.REJECTED,
        )
    ).scalar_one()
    assert history.actor == "SAGA"
    assert history.reason == "service is not published"


def test_reject_is_idempotent(
    activities: SchedulingActivities, db_session: Session, appointment: Appointment
) -> None:
    activities.reject(RejectInput(appointment.id, "reason A"))

    activities.reject(RejectInput(appointment.id, "reason B"))  # simulated retry

    db_session.refresh(appointment)
    assert appointment.status == AppointmentStatus.REJECTED
    assert (
        _history_count(db_session, appointment.id) == 2
    )  # not a duplicate REJECTED row


def test_validate_eligibility_flags_slot_belonging_to_a_different_provider(
    activities: SchedulingActivities,
    db_session: Session,
    appointment: Appointment,
    department,
    specialty,
) -> None:
    from app.models import Provider, User
    from app.models.enums import UserRole

    other_user = User(
        email="dr.other@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PROVIDER,
    )
    db_session.add(other_user)
    db_session.flush()
    other_provider = Provider(
        user_id=other_user.id,
        department_id=department.id,
        specialty_id=specialty.id,
        bio="Consultant, different provider.",
    )
    db_session.add(other_provider)
    db_session.flush()

    slot = db_session.get(Slot, appointment.slot_id)
    slot.provider_id = other_provider.id
    db_session.flush()

    with pytest.raises(ApplicationError) as exc_info:
        activities.validate_eligibility(appointment.id)

    assert "slot does not belong to this provider" in str(exc_info.value)


def test_schedule_reminders_queues_a_celery_task(
    worker_session: None,
    activities: SchedulingActivities,
    appointment: Appointment,
    db_session: Session,
) -> None:
    """schedule_reminders now queues the real Week 3 Celery task. Eager mode
    runs it inline in this same process; the worker_session fixture points
    session_scope() at db_session -- the same nullcontext trick the
    activities fixture above uses -- which is what lets this test see the
    notification it wrote, instead of it landing in a separate, invisible
    connection.
    """
    activities.schedule_reminders(appointment.id)

    notification = db_session.execute(
        select(Notification).where(
            Notification.payload["appointment_id"].astext == str(appointment.id)
        )
    ).scalar_one()
    assert notification.type == NotificationType.APPOINTMENT_REMINDER

"""Starts and reports on the Week 2 appointment-scheduling saga.

request_appointment creates the Appointment row and starts
AppointmentSchedulingWorkflow, mirroring service_publish.start_publish's
shape: write state, commit, then hand off to Temporal. get_appointment is
the single 404 entry point other modules (cancel, reschedule, the visit
lifecycle) will reuse.
"""

from fastapi import status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError
from redis import Redis
from app.models import (
    Appointment,
    AppointmentStatusHistory,
    Provider,
    Service,
    Slot,
    SlotReservation,
)
from app.models.enums import AppointmentStatus, SlotReservationStatus, SlotStatus
from app.schemas.appointment import AppointmentCreate
from app.services.idempotency import get_cached_result, store_result
from app.services.slot import reserve_slot_uncommitted
from app.services.waitlist import promote_next_waiting
from app.temporal.client import get_temporal_client
from app.temporal.workflows import AppointmentSchedulingWorkflow


def scheduling_workflow_id(appointment_id: int) -> str:
    """The deterministic Temporal workflow id for one appointment's saga."""
    return f"schedule-appointment-{appointment_id}"


def get_appointment(db: Session, appointment_id: int) -> Appointment:
    """Fetch one appointment by id, or raise 404."""
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="APPOINTMENT_NOT_FOUND",
            message="No appointment with that id.",
        )
    return appointment


def _get_by_idempotency_key(db: Session, idempotency_key: str) -> Appointment | None:
    return db.execute(
        select(Appointment).where(Appointment.idempotency_key == idempotency_key)
    ).scalar_one_or_none()


async def request_appointment(
    db: Session,
    redis_client: Redis,
    data: AppointmentCreate,
    patient_id: int,
    idempotency_key: str,
    actor: str,
) -> tuple[Appointment, str]:
    """Create an appointment and start its scheduling saga.

    Checks the Redis idempotency cache first, then the database, before
    creating anything -- the two mechanisms task 2.7 built. A repeat
    Idempotency-Key returns the original appointment; nothing new is
    created and no second saga starts.

    If starting the workflow itself fails (Temporal unreachable), the
    appointment is left REQUESTED rather than deleted -- the same accepted
    gap start_publish documents for the publish workflow, and for the same
    reason: this row can't be rolled back without violating "never delete
    an appointment."
    """
    cached = get_cached_result(redis_client, idempotency_key)
    if cached is not None:
        appointment = get_appointment(db, cached["appointment_id"])
        return appointment, scheduling_workflow_id(appointment.id)

    existing = _get_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return existing, scheduling_workflow_id(existing.id)

    if db.get(Provider, data.provider_id) is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="PROVIDER_NOT_FOUND",
            message="No provider with that id.",
        )
    if db.get(Slot, data.slot_id) is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="SLOT_NOT_FOUND",
            message="No slot with that id.",
        )
    if db.get(Service, data.service_id) is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="SERVICE_NOT_FOUND",
            message="No service with that id.",
        )

    appointment = Appointment(
        patient_id=patient_id,
        provider_id=data.provider_id,
        slot_id=data.slot_id,
        service_id=data.service_id,
        status=AppointmentStatus.REQUESTED,
        idempotency_key=idempotency_key,
    )
    db.add(appointment)
    try:
        db.commit()
    except IntegrityError:
        # Two requests racing on the same brand-new Idempotency-Key: the
        # loser's INSERT hits the unique constraint. Provider/slot/service
        # were already checked above, so this is the only remaining cause.
        db.rollback()
        existing = _get_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            return existing, scheduling_workflow_id(existing.id)
        raise
    db.refresh(appointment)

    db.add(
        AppointmentStatusHistory(
            appointment_id=appointment.id,
            from_status=None,
            to_status=AppointmentStatus.REQUESTED,
            actor=actor,
        )
    )
    db.commit()

    workflow_id = scheduling_workflow_id(appointment.id)
    client = await get_temporal_client()
    await client.start_workflow(
        AppointmentSchedulingWorkflow.run,
        appointment.id,
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )

    store_result(
        redis_client, idempotency_key, status.HTTP_202_ACCEPTED, appointment.id
    )

    return appointment, workflow_id


_CANCELLABLE_FROM = {
    AppointmentStatus.REQUESTED,
    AppointmentStatus.SLOT_RESERVED,
    AppointmentStatus.CONFIRMED,
}


def ensure_can_cancel(appointment: Appointment) -> None:
    """Raise 409 unless `appointment.status` allows cancelling.

    Mirrors ensure_can_publish/ensure_can_unpublish in service_publish.py:
    a standalone guard, checked once before the action starts, so every
    illegal entry action in this project fails the same way.
    """
    if appointment.status not in _CANCELLABLE_FROM:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="APPOINTMENT_NOT_CANCELLABLE",
            message=f"Cannot cancel an appointment with status {appointment.status}.",
        )


def cancel_appointment(db: Session, appointment_id: int, actor: str) -> Appointment:
    """Cancel an appointment: release its slot if one was held, transition
    to CANCELLED, and promote the next waitlist entry.

    A REQUESTED appointment never held a slot, so nothing is released and
    nobody is promoted; SLOT_RESERVED and CONFIRMED both did.

    actor is PATIENT/FRONT_DESK/ADMIN, never SAGA_COMPENSATION -- that
    label is reserved for the saga's own rollback after a billing
    failure. Same slot-release outcome, different reason recorded.
    """
    appointment = db.get(Appointment, appointment_id)
    ensure_can_cancel(appointment)

    if appointment.status in (
        AppointmentStatus.SLOT_RESERVED,
        AppointmentStatus.CONFIRMED,
    ):
        slot = db.get(Slot, appointment.slot_id)
        slot.status = SlotStatus.AVAILABLE

        reservation = db.execute(
            select(SlotReservation).where(
                SlotReservation.appointment_id == appointment.id,
                SlotReservation.slot_id == appointment.slot_id,
            )
        ).scalar_one()
        reservation.status = SlotReservationStatus.RELEASED

        promote_next_waiting(db, appointment.provider_id)

    db.add(
        AppointmentStatusHistory(
            appointment_id=appointment.id,
            from_status=appointment.status,
            to_status=AppointmentStatus.CANCELLED,
            actor=actor,
        )
    )
    appointment.status = AppointmentStatus.CANCELLED
    db.commit()
    db.refresh(appointment)
    return appointment


_RESCHEDULABLE_FROM = {AppointmentStatus.SLOT_RESERVED, AppointmentStatus.CONFIRMED}


def ensure_can_reschedule(appointment: Appointment) -> None:
    """Raise 409 unless `appointment.status` allows rescheduling.

    Narrower than ensure_can_cancel: REQUESTED never held a slot, so
    there is nothing to reschedule *from* -- the saga hasn't reserved
    anything yet, and the right move for a REQUESTED booking someone
    wants to change is to cancel it and submit a fresh request.
    """
    if appointment.status not in _RESCHEDULABLE_FROM:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="APPOINTMENT_NOT_RESCHEDULABLE",
            message=f"Cannot reschedule an appointment with status {appointment.status}.",
        )


def reschedule_appointment(
    db: Session, appointment_id: int, new_slot_id: int
) -> Appointment:
    """Move an appointment to a different slot with the same provider.

    Releasing the old slot and reserving the new one happen in one
    transaction: reserve_slot_uncommitted is called, not reserve_slot, so
    if the new slot can't be won, nothing commits at all -- not the old
    slot's release, not the waitlist promotion. The appointment is left
    holding its original slot, never neither and never both.

    appointment.status is left exactly as it was. A time change is not a
    billing event or a saga re-run: CONFIRMED stays CONFIRMED,
    SLOT_RESERVED stays SLOT_RESERVED, just pointing at a new slot. A
    CONFIRMED appointment's new slot is set straight to BOOKED (not
    RESERVED) and its reservation to COMMITTED, matching what confirm()
    already means for those columns.
    """
    appointment = db.get(Appointment, appointment_id)
    ensure_can_reschedule(appointment)

    new_slot = db.get(Slot, new_slot_id)
    if new_slot is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="SLOT_NOT_FOUND",
            message="No slot with that id.",
        )
    if new_slot.provider_id != appointment.provider_id:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="SLOT_WRONG_PROVIDER",
            message="The new slot does not belong to this appointment's provider.",
        )

    old_slot = db.get(Slot, appointment.slot_id)
    old_slot.status = SlotStatus.AVAILABLE
    old_reservation = db.execute(
        select(SlotReservation).where(
            SlotReservation.appointment_id == appointment.id,
            SlotReservation.slot_id == appointment.slot_id,
        )
    ).scalar_one()
    old_reservation.status = SlotReservationStatus.RELEASED
    promote_next_waiting(db, appointment.provider_id)

    won = reserve_slot_uncommitted(db, new_slot_id)
    if not won:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="SLOT_UNAVAILABLE",
            message="The new slot is no longer available.",
        )

    if appointment.status == AppointmentStatus.CONFIRMED:
        new_slot.status = SlotStatus.BOOKED
        new_reservation_status = SlotReservationStatus.COMMITTED
    else:
        new_reservation_status = SlotReservationStatus.RESERVED

    db.add(
        SlotReservation(
            appointment_id=appointment.id,
            slot_id=new_slot_id,
            status=new_reservation_status,
        )
    )
    appointment.slot_id = new_slot_id

    db.commit()
    db.refresh(appointment)
    return appointment

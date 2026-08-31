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
    Patient,
    Provider,
    Service,
    Slot,
    User,
)
from app.models.enums import AppointmentStatus, UserRole
from app.schemas.appointment import AppointmentCreate
from app.services.idempotency import get_cached_result, store_result
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


def resolve_booking_patient(
    db: Session, current_user: User, data: AppointmentCreate
) -> tuple[Patient, str]:
    """Decide whose appointment this is, and what to record as the actor.

    A PATIENT always books for themselves: data.patient_id is ignored
    rather than trusted, so setting it is not a way to book in someone
    else's name. FRONT_DESK/ADMIN book on a patient's behalf and must say
    which patient.

    The actor label is just the caller's role. Day 5 fixed the history
    vocabulary as PATIENT/FRONT_DESK/PROVIDER/ADMIN plus SAGA and
    SAGA_COMPENSATION -- the four human actors are the four roles by
    design, so there is nothing to map between.
    """
    if current_user.role == UserRole.PATIENT:
        patient = db.execute(
            select(Patient).where(Patient.user_id == current_user.id)
        ).scalar_one()
        return patient, current_user.role.value

    if data.patient_id is None:
        raise AppError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="PATIENT_ID_REQUIRED",
            message="patient_id is required when booking on a patient's behalf.",
        )
    patient = db.get(Patient, data.patient_id)
    if patient is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="PATIENT_NOT_FOUND",
            message="No patient with that id.",
        )
    return patient, current_user.role.value


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

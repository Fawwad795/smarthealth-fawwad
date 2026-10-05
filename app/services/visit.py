"""The visit lifecycle: CHECKED_IN -> IN_PROGRESS -> COMPLETED.

Deliberately not a Temporal workflow. Each move is a separate human
action taken at its own pace, so nothing is ever mid-flight between them
and there is nothing to resume. All this needs is a status column and one
guard per transition -- the same shape service_publish.py uses.

Idempotency and illegal jumps are different questions, handled
separately: repeating a transition you already made returns the visit
untouched, while asking for one that skips or reverses a state raises.
"""

from datetime import UTC, datetime

from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.events.envelope import EventType
from app.events.outbox import record_event
from app.models import Appointment, AppointmentStatusHistory, Visit
from app.models.enums import AppointmentStatus, VisitStatus
from app.services.appointment_scheduling import get_appointment


def _get_visit_or_none(db: Session, appointment_id: int) -> Visit | None:
    return db.execute(
        select(Visit).where(Visit.appointment_id == appointment_id)
    ).scalar_one_or_none()


def get_visit(db: Session, appointment_id: int) -> Visit:
    """Fetch this appointment's visit, or raise 404.

    Keyed on appointment_id rather than a visit id: a caller always has
    the appointment in hand, and the 1:1 makes the two interchangeable.
    A missing row means nobody was ever checked in -- which is exactly
    the brief's own example of an illegal jump when it is start or
    complete that asked.
    """
    visit = _get_visit_or_none(db, appointment_id)
    if visit is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="VISIT_NOT_FOUND",
            message="This appointment has not been checked in.",
        )
    return visit


def ensure_can_check_in(appointment: Appointment) -> None:
    """Raise 409 unless this appointment can start a visit."""
    if appointment.status != AppointmentStatus.CONFIRMED:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="APPOINTMENT_NOT_CONFIRMED",
            message=(
                f"Cannot check in an appointment with status {appointment.status}. "
                "Only a CONFIRMED appointment can start a visit."
            ),
        )


def ensure_can_start(visit: Visit) -> None:
    """Raise 409 unless this visit can move to IN_PROGRESS.

    Only COMPLETED blocks it -- a visit never moves backward. Already
    being IN_PROGRESS passes here deliberately: start_visit returns early
    for that case before this guard runs, because repeating a transition
    is a retry, not a conflict.
    """
    if visit.status == VisitStatus.COMPLETED:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="VISIT_ALREADY_COMPLETED",
            message="Cannot move a completed visit back to in-progress.",
        )


def ensure_can_complete(visit: Visit) -> None:
    """Raise 409 unless this visit can move to COMPLETED.

    CHECKED_IN blocks it: completing a visit that never started is the
    brief's named example of an illegal jump. Already being COMPLETED
    passes deliberately -- same reasoning as ensure_can_start.
    """
    if visit.status == VisitStatus.CHECKED_IN:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="VISIT_NOT_IN_PROGRESS",
            message="Cannot complete a visit that has not started.",
        )


def check_in(db: Session, appointment_id: int) -> Visit:
    """Check a patient in, creating their visit. Idempotent.

    An existing visit is returned untouched whatever state it has since
    reached: a retried check-in must not create a second row, and must
    not reset one that has already moved on. That check runs first, so a
    retry never depends on the appointment still being CONFIRMED -- by
    the time a visit completes, it is not.
    """
    existing = _get_visit_or_none(db, appointment_id)
    if existing is not None:
        return existing

    appointment = get_appointment(db, appointment_id)
    ensure_can_check_in(appointment)

    visit = Visit(
        appointment_id=appointment_id,
        status=VisitStatus.CHECKED_IN,
        checked_in_at=datetime.now(UTC),
    )
    db.add(visit)
    db.commit()
    db.refresh(visit)
    return visit


def start_visit(db: Session, appointment_id: int) -> Visit:
    """Move a visit to IN_PROGRESS. Idempotent if already there."""
    visit = get_visit(db, appointment_id)
    if visit.status == VisitStatus.IN_PROGRESS:
        return visit
    ensure_can_start(visit)

    visit.status = VisitStatus.IN_PROGRESS
    db.commit()
    db.refresh(visit)
    return visit


def complete_visit(db: Session, appointment_id: int, actor: str) -> Visit:
    """Complete a visit, and its appointment with it. Idempotent.

    The one visit transition that also moves Appointment.status, so
    unlike check-in and start it writes an appointment_status_history
    row: CONFIRMED -> COMPLETED is a real appointment transition, which a
    reschedule's slot move never was.

    Week 3 hangs the billing update, the follow-up reminder and the
    analytics rollup off this same point, outside the request.
    """
    visit = get_visit(db, appointment_id)
    if visit.status == VisitStatus.COMPLETED:
        return visit
    ensure_can_complete(visit)

    visit.status = VisitStatus.COMPLETED
    visit.completed_at = datetime.now(UTC)

    appointment = visit.appointment
    db.add(
        AppointmentStatusHistory(
            appointment_id=appointment.id,
            from_status=appointment.status,
            to_status=AppointmentStatus.COMPLETED,
            actor=actor,
        )
    )
    appointment.status = AppointmentStatus.COMPLETED

    record_event(
        db,
        EventType.VISIT_COMPLETED,
        visit.id,
        {"visit_id": visit.id, "appointment_id": appointment.id},
    )

    db.commit()
    db.refresh(visit)
    return visit

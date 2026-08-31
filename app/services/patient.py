"""Resolving which patient a self-or-on-behalf request is acting for.

Booking an appointment and joining a waitlist ask the identical question:
a PATIENT acts for themselves, while FRONT_DESK/ADMIN act on someone
else's behalf and must say whose. This logic started inside
appointment_scheduling.py; the waitlist was the second caller, which is
what showed the rule was never appointment-specific.
"""

from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models import Patient, User
from app.models.enums import UserRole


def resolve_acting_patient(
    db: Session, current_user: User, patient_id: int | None
) -> tuple[Patient, str]:
    """Decide which patient this request is for, and what to record as the actor.

    A PATIENT always acts for themselves: patient_id is ignored rather
    than trusted, so sending someone else's is not a way to act in their
    name. FRONT_DESK/ADMIN act on a patient's behalf and must name one.

    The actor label is just the caller's role. Day 5 fixed the history
    vocabulary as PATIENT/FRONT_DESK/PROVIDER/ADMIN plus SAGA and
    SAGA_COMPENSATION -- the human actors are the roles by design, so
    there is nothing to map between.
    """
    if current_user.role == UserRole.PATIENT:
        patient = db.execute(
            select(Patient).where(Patient.user_id == current_user.id)
        ).scalar_one()
        return patient, current_user.role.value

    if patient_id is None:
        raise AppError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="PATIENT_ID_REQUIRED",
            message="patient_id is required when acting on a patient's behalf.",
        )
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="PATIENT_NOT_FOUND",
            message="No patient with that id.",
        )
    return patient, current_user.role.value

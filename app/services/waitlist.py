"""Joining a provider's waitlist.

What *moves* the queue is cancelling an appointment, which arrives in the
next subtask -- this module only handles getting into it.
"""

from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models import Provider, Waitlist
from app.models.enums import WaitlistStatus


def join_waitlist(db: Session, provider_id: int, patient_id: int) -> Waitlist:
    """Add this patient to this provider's queue, or 409 if already in it.

    Checked before inserting so a duplicate gets a clean 409 rather than
    an IntegrityError the handler would flatten into a 500;
    uq_waitlist_one_waiting_entry is the backstop for two concurrent joins
    that both pass this check. Same check-plus-constraint pairing the
    appointment idempotency key uses.
    """
    if db.get(Provider, provider_id) is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="PROVIDER_NOT_FOUND",
            message="No provider with that id.",
        )

    existing = db.execute(
        select(Waitlist).where(
            Waitlist.provider_id == provider_id,
            Waitlist.patient_id == patient_id,
            Waitlist.status == WaitlistStatus.WAITING,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="ALREADY_ON_WAITLIST",
            message="This patient is already waiting for this provider.",
        )

    entry = Waitlist(
        provider_id=provider_id,
        patient_id=patient_id,
        status=WaitlistStatus.WAITING,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

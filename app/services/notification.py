"""Notification business logic: what actually happens when something is sent.

Thin Celery tasks in app/workers/tasks/ call into here -- the same
router-vs-service split FastAPI endpoints use, just with a task decorator
instead of a route decorator on the thin side.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Appointment, Notification, Patient
from app.models.enums import NotificationStatus, NotificationType

logger = logging.getLogger(__name__)


def send_appointment_reminder(db: Session, appointment_id: int) -> None:
    """Write a reminder notification for one appointment, unless one already exists.

    The existence check is what makes this safe to call twice -- Celery's
    at-least-once delivery means it sometimes will be. A plain SELECT before
    INSERT, not a unique constraint: a duplicate reminder slipping through a
    genuine race is a low-stakes, documented limitation here, unlike slot
    booking where the same kind of gap would be a real double-booking.
    """
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise ValueError(f"appointment {appointment_id} not found")

    patient = db.get(Patient, appointment.patient_id)

    already_sent = db.execute(
        select(Notification.id).where(
            Notification.user_id == patient.user_id,
            Notification.type == NotificationType.APPOINTMENT_REMINDER,
            Notification.payload["appointment_id"].astext == str(appointment_id),
        )
    ).first()

    if already_sent is not None:
        logger.info("reminder already sent, skipping appointment_id=%s", appointment_id)
        return

    db.add(
        Notification(
            user_id=patient.user_id,
            type=NotificationType.APPOINTMENT_REMINDER,
            payload={"appointment_id": appointment_id},
            status=NotificationStatus.SENT,
        )
    )
    db.commit()
    logger.info("reminder sent appointment_id=%s", appointment_id)

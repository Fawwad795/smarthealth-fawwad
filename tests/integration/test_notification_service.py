"""Tests for app/services/notification.py: what "sending" a reminder means,
and why running it twice is safe.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Appointment, Notification, User
from app.models.enums import NotificationStatus, NotificationType
from app.services.notification import send_appointment_reminder


def test_send_appointment_reminder_writes_a_notification(
    db_session: Session, appointment: Appointment, patient_user: User
) -> None:
    send_appointment_reminder(db_session, appointment.id)

    notification = db_session.execute(select(Notification)).scalar_one()
    assert notification.user_id == patient_user.id
    assert notification.type == NotificationType.APPOINTMENT_REMINDER
    assert notification.status == NotificationStatus.SENT
    assert notification.payload == {"appointment_id": appointment.id}


def test_send_appointment_reminder_is_idempotent(
    db_session: Session, appointment: Appointment
) -> None:
    """Calling it twice must not write a second row -- Celery's at-least-once
    delivery means it sometimes genuinely will be.
    """
    send_appointment_reminder(db_session, appointment.id)
    send_appointment_reminder(db_session, appointment.id)

    notifications = db_session.execute(select(Notification)).scalars().all()
    assert len(notifications) == 1


def test_send_appointment_reminder_raises_for_a_missing_appointment(
    db_session: Session,
) -> None:
    with pytest.raises(ValueError, match="not found"):
        send_appointment_reminder(db_session, 999999999)

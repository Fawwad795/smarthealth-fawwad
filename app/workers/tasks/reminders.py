"""Celery tasks for patient/staff notifications.

Thin wrappers only -- app/services/notification.py has the actual logic.
Each task opens its own DB session (SessionLocal), the same pattern Temporal
Activities use, since neither runs inside a FastAPI request with
Depends(get_db) available.
"""

from sqlalchemy.exc import OperationalError

from app.db.session import SessionLocal
from app.services import notification as notification_service
from app.workers.base import DeadLetterTask
from app.workers.celery_app import celery_app


@celery_app.task(
    base=DeadLetterTask,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_appointment_reminder(appointment_id: int) -> None:
    """Queue-facing wrapper: opens a session, delegates, closes it."""
    with SessionLocal() as db:
        notification_service.send_appointment_reminder(db, appointment_id)

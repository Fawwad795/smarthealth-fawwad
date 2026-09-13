"""Celery tasks for patient/staff notifications.

Thin wrappers only -- app/services/notification.py has the actual logic.
Each task opens its own DB session (session_scope), the same pattern Temporal
Activities use, since neither runs inside a FastAPI request with
Depends(get_db) available.
"""

from sqlalchemy.exc import OperationalError

from app.celery.base import DeadLetterTask
from app.celery.celery_app import celery_app
from app.core.logging import set_correlation_id
from app.db.session import session_scope
from app.services import notification as notification_service


@celery_app.task(
    base=DeadLetterTask,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_appointment_reminder(
    appointment_id: int, correlation_id: str | None = None
) -> None:
    """Queue-facing wrapper: opens a session, delegates, closes it.

    correlation_id arrives as an ordinary task argument because a message
    on the broker is the only thing that crosses from the process that
    queued this to the one running it -- a ContextVar set by the caller is
    invisible here. Re-establishing it first is what makes every line this
    task and the service beneath it writes carry the id of the request that
    caused it.

    Setting it *unconditionally* matters more than it looks: a prefork
    worker runs one task after another in the same process, so a task that
    skipped this would silently inherit the previous task's id and file its
    logs under someone else's booking. None mints a fresh one, which is
    both correct for a task with no upstream request and the thing that
    stops the leak.
    """
    set_correlation_id(correlation_id)
    with session_scope() as db:
        notification_service.send_appointment_reminder(db, appointment_id)

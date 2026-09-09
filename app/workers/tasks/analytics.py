"""Celery tasks for analytics: the periodic rollup Celery Beat schedules.

Recomputes today's analytics_daily row every few minutes until task 3.4's
Kafka consumer exists to also keep it fresh in near-real-time.
"""

from datetime import UTC, datetime

from sqlalchemy.exc import OperationalError

from app.core.logging import set_correlation_id
from app.db.session import session_scope
from app.services import analytics as analytics_service
from app.workers.base import DeadLetterTask
from app.workers.celery_app import celery_app


@celery_app.task(
    base=DeadLetterTask,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def rollup_today() -> None:
    """Recompute today's analytics_daily row. Beat's scheduled entry point."""
    # Beat-scheduled, so there is no upstream request to inherit from: this
    # run mints its own id, which both gives the run a traceable identity
    # and stops it picking up a leftover id from whatever this worker
    # process ran before it.
    set_correlation_id(None)
    with session_scope() as db:
        analytics_service.rollup_analytics_for_date(db, datetime.now(UTC).date())

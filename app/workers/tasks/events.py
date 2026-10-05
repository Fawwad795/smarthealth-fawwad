"""The Celery task that drains the outbox on a timer.

A thin wrapper, same as every other task here -- app/events/relay.py has
the logic.
"""

import logging

from confluent_kafka import KafkaException
from sqlalchemy.exc import OperationalError

from app.core.logging import set_correlation_id
from app.db.session import session_scope
from app.events import relay
from app.workers.base import DeadLetterTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    base=DeadLetterTask,
    # Both are transient by nature: a broker that is briefly unreachable,
    # or a dropped database connection. Neither is a reason to give up on
    # events that are still safely sitting in the table.
    autoretry_for=(KafkaException, OperationalError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def publish_outbox_events() -> None:
    """Publish whatever is waiting. Beat's scheduled entry point."""
    # This run's own id, for the relay's log lines. It is not the events'
    # id -- each envelope already carries the correlation id of whatever
    # request created it, which is the one that matters for tracing.
    set_correlation_id(None)

    with session_scope() as db:
        published = relay.publish_pending_events(db)

    if published:
        logger.info("published outbox events count=%s", published)

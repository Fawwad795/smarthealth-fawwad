"""Draining the outbox: the courier half of the pattern.

Reads events queued by app/events/outbox.py, publishes them, and stamps
published_at. The only writer of the published_at column.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events.envelope import build_envelope, topic_for
from app.kafka.producer import publish
from app.models import OutboxEvent

logger = logging.getLogger(__name__)

# Small enough that one failure re-sends little, large enough that a burst
# of bookings drains in one pass.
BATCH_SIZE = 100


def publish_pending_events(db: Session, limit: int = BATCH_SIZE) -> int:
    """Publish unpublished events, oldest first. Returns how many were sent.

    The claim is FOR UPDATE ... SKIP LOCKED so two overlapping relay runs
    never fight over the same row: the second simply skips what the first
    has locked, rather than blocking on it or publishing it twice. Same
    reasoning as the atomic slot reservation -- let the database arbitrate
    instead of checking and then acting.

    published_at is stamped only *after* the broker acknowledges. Marking
    first would recreate, one layer up, the exact failure the outbox
    exists to prevent: a row claiming success for a message that never
    arrived, with nothing left to retry it.

    A failure part-way through rolls the whole batch back, so already-sent
    messages are sent again on the next run. That is at-least-once
    delivery and it is the accepted trade -- duplicates are absorbed by
    the consumer's processed_events table (task 3.5). Exactly-once is not
    available here, or anywhere.
    """
    rows = (
        db.execute(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )

    for row in rows:
        publish(topic_for(row.event_type), str(row.aggregate_id), build_envelope(row))
        row.published_at = datetime.now(UTC)

    db.commit()
    return len(rows)

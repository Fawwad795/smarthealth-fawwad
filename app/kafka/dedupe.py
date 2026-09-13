"""Claiming an event, so a second delivery changes nothing.

Kafka delivers at-least-once, and the outbox relay republishes any batch
it failed part-way through, so the same event_id arriving twice is
routine rather than exceptional. This is the single place that decides
whether an arrival is the first one.
"""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import ProcessedEvent


def claim_event(db: Session, event_id: str, consumer: str) -> bool:
    """Record this consumer as handling event_id. False means it already had.

    ON CONFLICT DO NOTHING rather than catching IntegrityError, and the
    difference is not stylistic. A violated constraint aborts the entire
    transaction in Postgres -- every subsequent statement is refused
    until a rollback -- and the caller must run its handler inside *this*
    transaction, so that the claim and the aggregate update commit
    together. Catching the error would poison the very transaction the
    handler is about to use; escaping that would mean a SAVEPOINT around
    every claim, for no gain.

    RETURNING is what makes the answer readable. DO NOTHING yields no row
    when the insert was suppressed, so "did a row come back?" answers
    "was this the first delivery?" in one statement -- no second SELECT,
    and no gap between checking and acting.

    Deliberately does not commit, exactly as record_event() does not: the
    caller is mid-transaction, and this claim must land or roll back with
    the work it guards.
    """
    claimed = db.execute(
        insert(ProcessedEvent)
        .values(consumer=consumer, event_id=event_id)
        .on_conflict_do_nothing(index_elements=["consumer", "event_id"])
        .returning(ProcessedEvent.event_id)
    ).scalar_one_or_none()

    return claimed is not None

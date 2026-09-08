"""ProcessedEvent: the consumer's record of what it has already handled.

Kafka delivers at-least-once, and yesterday's outbox relay makes that
concrete: a batch that fails part-way rolls back and republishes rows the
broker had already accepted. Duplicates were the accepted price of never
losing an event -- this table is where that price is paid.

The guard is an INSERT, not a SELECT. A lookup followed by a decision has
a gap between the two, and two consumers running at once can both look,
both see nothing, and both process. The unique constraint closes the gap
by making the check and the act a single statement -- the same reasoning
as the Week 2 slot reservation.
"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ProcessedEvent(Base, TimestampMixin):
    """One event, marked as handled by one consumer.

    The row must be inserted in the same transaction as the aggregate
    update it guards. Committing the marker on its own would let a crash
    leave an event flagged handled that was never handled -- and since
    the guard then skips it forever, that turns a harmless duplicate into
    a permanently lost event, which is the worse of the two failures.

    The primary key is (consumer, event_id) rather than a surrogate id:
    nothing references this table by foreign key and the pair is already
    unique, so an id column would identify nothing. Same reasoning
    AnalyticsDaily uses for keying on date.

    consumer is part of that key rather than merely recorded beside it.
    Idempotency is per-consumer -- each consumer must handle an event
    exactly once -- so keying on event_id alone would mean a second
    consumer silently skipped everything the first had already seen.

    There is deliberately no processed_at column: the marker and the
    aggregate update commit together, so created_at already is that
    moment. Two columns holding one fact can drift apart; one cannot.
    """

    __tablename__ = "processed_events"

    # The consumer group that handled it -- settings.kafka_consumer_group,
    # e.g. "app-analytics".
    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)

    # The envelope's event_id, generated in Python when the outbox row was
    # written -- so a republished event arrives carrying the same id it
    # had the first time, which is the only reason a duplicate is
    # recognisable at all.
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)

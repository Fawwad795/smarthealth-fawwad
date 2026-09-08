"""OutboxEvent: an event waiting to be published to Kafka.

The row is written in the same transaction as the business change that
caused it, which is the whole point -- see docs/events.md. A relay publishes
unpublished rows afterwards and stamps published_at.

Saving to Postgres and producing to Kafka cannot be made atomic with each
other; committing first can lose an event on a crash, producing first can
announce something that then rolls back. Turning the publish into a database
write removes the gap: the event and the change it describes now commit or
roll back together.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class OutboxEvent(Base, TimestampMixin):
    """One event queued for publication.

    created_at doubles as the envelope's occurred_at. A separate column
    would hold the same moment twice, and created_at is filled by Postgres
    -- so every event across all four processes is stamped by one clock,
    the reasoning TimestampMixin already sets out.
    """

    __tablename__ = "outbox_events"

    # A partial index: it holds only the unpublished rows, which is the one
    # query the relay ever runs. It therefore stays small no matter how far
    # the table grows, where a plain index on published_at would keep
    # indexing millions of already-published rows nobody looks up.
    __table_args__ = (
        Index(
            "ix_outbox_events_unpublished",
            "id",
            postgresql_where=(mapped_column("published_at").is_(None)),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # The envelope's event_id: generated in Python at record time, not by
    # the database, so it is fixed before the row is even inserted and a
    # republished row carries the same id. That is what lets a consumer
    # recognise a duplicate (task 3.5).
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)

    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # The Kafka message key -- the id of the appointment/visit/service this
    # is about. Same key means same partition means guaranteed ordering.
    aggregate_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Ids only. Never a name, contact detail or free text (rule 6.6).
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # NULL means "not yet published". Set by the relay after the broker
    # acknowledges the message.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

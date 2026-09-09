"""ContentChunk: one searchable fragment of text produced by publishing a
service, the output of the Week 2 publish workflow's chunk Activity.

Part B's retrieval reads this table (and, once embeddings exist, the
vector column/table it joins to) -- this project never chunks anything but
services, but the columns stay generic per the brief's data model.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ContentSourceType, enum_column
from app.models.mixins import TimestampMixin


class ContentChunk(Base, TimestampMixin):
    """A single chunk of a source's content, addressed by (source_type,
    source_id, chunk_index) rather than a foreign key.

    No FK to services.id: source_type is what lets this table point at
    more than one kind of source in principle, and a foreign key can only
    ever point at one specific table. The chunk Activity that writes these
    rows is responsible for confirming the source it names actually
    exists -- the check the database would otherwise do for free.
    """

    __tablename__ = "content_chunks"
    __table_args__ = (UniqueConstraint("source_type", "source_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # The composite unique constraint below indexes (source_type, source_id,
    # chunk_index) as a B-tree, so a query filtered on source_type alone, or
    # on (source_type, source_id), already has a leftmost-prefix match -- no
    # separate index= needed on either column.
    source_type: Mapped[ContentSourceType] = mapped_column(
        enum_column(ContentSourceType, "source_type"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # sha256 hex digest of `text`. Week 4 task 4.9 skips re-embedding a
    # chunk whose hash hasn't changed since the last publish.
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Null until Part B's embedding Activity runs. Week 2 populates every
    # other column and leaves this one alone.
    embedded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

"""ChunkEmbedding: the vector for one content chunk, plus the metadata
search filters and reports on.

Written by Week 4's embedding Activity, read by POST /search. Kept in its
own table rather than as a column on content_chunks, following the brief's
data model: a chunk exists from the moment a service is published, its
vector only once the embedding provider has answered.
"""

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin

# The size of every stored vector. all-MiniLM-L6-v2 returns 384 numbers per
# text. The column is created with this size, so Postgres rejects a vector of
# any other length; switching to a model with a different size needs a
# migration as well as a change to EMBEDDING_DIMENSIONS in the environment.
EMBEDDING_DIMENSIONS = 384


class ChunkEmbedding(Base, TimestampMixin):
    """One chunk's vector, the model that produced it, and the service
    metadata the brief requires every stored vector to carry: service_id,
    department, specialty and a published flag.

    department, specialties and published are copies of what lives on
    the service. They travel with the vector because the store sits behind
    a swappable interface (app/ai/vector_store.py), and a store such as
    Qdrant has no join to fetch them at search time.
    """

    __tablename__ = "chunk_embeddings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # The one exception to this schema's ON DELETE RESTRICT rule. A vector is
    # computed from its chunk's text and means nothing once that text is
    # gone, and chunk_content deletes a service's chunks on every publish.
    # CASCADE makes the database itself remove the old vector in the same
    # statement, so no re-publish can leave an orphan behind. Unique: one
    # vector per chunk, replaced rather than added to when re-embedded.
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("content_chunks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=False
    )

    # Which model produced the vector. Vectors from two different models
    # cannot be compared, so a later model change can find every stale row.
    model: Mapped[str] = mapped_column(String(200), nullable=False)

    # A real foreign key, unlike content_chunks.source_id, which stays
    # generic. Indexed because the store replaces and re-flags vectors by
    # service.
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # The department's name and the linked providers' specialties when the
    # vector was written -- the same values the chunk text was built from.
    # A list, because a service can be delivered by providers of several
    # specialties.
    department: Mapped[str] = mapped_column(String(100), nullable=False)
    specialties: Mapped[list[str]] = mapped_column(ARRAY(String(100)), nullable=False)

    # A boolean, not a status enum: services.status holds the lifecycle, and
    # this only answers "may search return this vector right now?". The
    # vector store writes every new vector False, while the service is still
    # PUBLISHING, and mark_published sets it True in the same transaction as
    # the status.
    published: Mapped[bool] = mapped_column(Boolean, nullable=False)

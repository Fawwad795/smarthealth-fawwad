"""Stores embedding vectors, behind one small interface.

The brief asks for the vector store to sit behind an interface so it can be
swapped. Callers (the embedding Activity in task 4.5, mark_published) use
VectorStore and get one from get_vector_store(). PgVectorStore, the only
implementation, writes to the chunk_embeddings table in the existing
Postgres. Search joins the interface with task 4.8.

Neither method commits. The caller owns the transaction, so a service's
status and its vectors change together or not at all.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.models import ChunkEmbedding


@dataclass(frozen=True)
class VectorRecord:
    """One chunk's vector and the metadata stored beside it.

    There is no published field: every vector is stored unpublished, and
    set_published() is the only way to make one searchable. That keeps a
    vector out of search until its service's publish has finished.
    """

    chunk_id: int
    embedding: list[float]
    model: str
    department: str
    specialties: list[str]


class VectorStore(ABC):
    """The contract every vector store implementation keeps."""

    @abstractmethod
    def replace_service_vectors(
        self, service_id: int, records: list[VectorRecord]
    ) -> None:
        """Remove every vector stored for this service and store `records`
        in their place, unpublished.

        Replace, never add: running it twice leaves the same rows as running
        it once. That is what makes a retried Activity safe, and what stops
        a re-publish leaving a stale or duplicate vector behind.
        """

    @abstractmethod
    def set_published(self, service_id: int, published: bool) -> None:
        """Set the published flag on every vector of this service."""


class PgVectorStore(VectorStore):
    """VectorStore over the chunk_embeddings table, using the caller's Session."""

    def __init__(self, db: Session) -> None:
        """Keep the caller's session; every write joins its transaction."""
        self._db = db

    def replace_service_vectors(
        self, service_id: int, records: list[VectorRecord]
    ) -> None:
        """Delete this service's vectors, then insert `records`.

        Deleting by service, not upserting chunk by chunk, means the vector
        of a chunk that has been dropped cannot survive. Here the CASCADE on
        chunk_id already guarantees that; the interface promises it for any
        store, including one with no foreign keys.
        """
        self._db.execute(
            delete(ChunkEmbedding).where(ChunkEmbedding.service_id == service_id)
        )
        self._db.add_all(
            ChunkEmbedding(
                chunk_id=record.chunk_id,
                service_id=service_id,
                embedding=record.embedding,
                model=record.model,
                department=record.department,
                specialties=record.specialties,
                published=False,
            )
            for record in records
        )
        # Flush so a bad row (a vector of the wrong size, a chunk that
        # already has one) fails here, inside the store, not at the
        # caller's commit.
        self._db.flush()

    def set_published(self, service_id: int, published: bool) -> None:
        """One UPDATE for all of this service's vectors."""
        self._db.execute(
            update(ChunkEmbedding)
            .where(ChunkEmbedding.service_id == service_id)
            .values(published=published)
        )


def get_vector_store(db: Session) -> VectorStore:
    """The one place that picks the vector store implementation, so
    swapping stores means changing this function and nothing that calls it.
    """
    return PgVectorStore(db)

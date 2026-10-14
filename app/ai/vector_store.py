"""Stores embedding vectors and searches them, behind one small interface.

The brief asks for the vector store to sit behind an interface so it can be
swapped. Callers (the embedding Activity in task 4.5, mark_published,
start_publish and unpublish, and search in task 4.8) use VectorStore and get
one from get_vector_store(). PgVectorStore, the only implementation, uses
the chunk_embeddings table in the existing Postgres.

No method commits. The caller owns the transaction, so a service's status
and its vectors change together or not at all.
"""

from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import delete, or_, select, update
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


@dataclass(frozen=True)
class VectorHit:
    """One service a search found: its id, the metadata stored on its
    vector, and how close the query came, as a cosine similarity (1 is the
    same direction, 0 is unrelated)."""

    service_id: int
    department: str
    specialties: list[str]
    similarity: float


class VectorStore(ABC):
    """The contract every vector store implementation keeps."""

    @abstractmethod
    def search(
        self,
        query_embedding: list[float],
        model: str,
        top_k: int,
        min_similarity: float,
    ) -> list[VectorHit]:
        """The `top_k` published services nearest the query, most similar
        first, each scoring at least `min_similarity`.

        Only published vectors are searched, and only those made by `model`,
        the model that embedded the query: vectors from another model are
        not comparable with it. A service with several chunks appears once,
        at its best chunk's score. Fewer than `top_k`, or none, is a valid
        answer: nothing close enough.
        """

    @abstractmethod
    def embedded_chunk_ids(self, service_id: int, model: str) -> set[int]:
        """The ids of this service's chunks that already have a vector made
        by `model`.

        The embedding Activity skips these (task 4.9). A vector made by any
        other model does not count: two models place text in unrelated
        spaces, so its distance to a new query's vector means nothing.
        """

    @abstractmethod
    def store_service_vectors(
        self,
        service_id: int,
        records: list[VectorRecord],
        current_chunk_ids: Collection[int],
    ) -> None:
        """Store `records` unpublished, each replacing any vector its chunk
        already has, and remove every vector of this service whose chunk is
        not in `current_chunk_ids`.

        A vector whose chunk is current and not in `records` is kept as it
        is: that is the vector task 4.9 avoids recomputing. Every chunk in
        `records` must be in `current_chunk_ids`.

        Running it twice leaves the same rows as running it once, which is
        what makes a retried Activity safe and stops a re-publish leaving a
        stale or duplicate vector behind.
        """

    @abstractmethod
    def set_published(self, service_id: int, published: bool) -> None:
        """Set the published flag on every vector of this service."""


class PgVectorStore(VectorStore):
    """VectorStore over the chunk_embeddings table, using the caller's Session."""

    def __init__(self, db: Session) -> None:
        """Keep the caller's session; every write joins its transaction."""
        self._db = db

    def search(
        self,
        query_embedding: list[float],
        model: str,
        top_k: int,
        min_similarity: float,
    ) -> list[VectorHit]:
        """Two SELECTs nested: the inner keeps each service's nearest chunk,
        the outer ranks those and takes `top_k`.

        Every filter is a WHERE clause, so Postgres applies it before
        ranking: an unpublished vector or a weak match never takes one of
        the `top_k` places. pgvector's `<=>` is cosine distance, which is
        1 - similarity, so the threshold becomes a maximum distance.
        """
        distance = ChunkEmbedding.embedding.cosine_distance(query_embedding)
        # DISTINCT ON (service_id), ordered by distance within each service,
        # keeps one row per service: its nearest chunk. Today every service
        # has exactly one chunk (chunk_content), so this changes nothing
        # until a service has several.
        nearest_per_service = (
            select(
                ChunkEmbedding.service_id,
                ChunkEmbedding.department,
                ChunkEmbedding.specialties,
                distance.label("distance"),
            )
            .where(
                ChunkEmbedding.published.is_(True),
                ChunkEmbedding.model == model,
                distance <= 1 - min_similarity,
            )
            .distinct(ChunkEmbedding.service_id)
            .order_by(ChunkEmbedding.service_id, distance)
            .subquery()
        )
        rows = self._db.execute(
            select(nearest_per_service)
            # service_id breaks ties, so equal scores come back in the same
            # order every time.
            .order_by(
                nearest_per_service.c.distance, nearest_per_service.c.service_id
            ).limit(top_k)
        ).all()
        return [
            VectorHit(
                service_id=row.service_id,
                department=row.department,
                specialties=list(row.specialties),
                similarity=1 - row.distance,
            )
            for row in rows
        ]

    def embedded_chunk_ids(self, service_id: int, model: str) -> set[int]:
        """One SELECT of chunk ids, filtered on service and model."""
        return set(
            self._db.scalars(
                select(ChunkEmbedding.chunk_id).where(
                    ChunkEmbedding.service_id == service_id,
                    ChunkEmbedding.model == model,
                )
            )
        )

    def store_service_vectors(
        self,
        service_id: int,
        records: list[VectorRecord],
        current_chunk_ids: Collection[int],
    ) -> None:
        """One DELETE for every vector being replaced or no longer current,
        then one INSERT per record.

        Here the CASCADE on chunk_id already removes the vector of a deleted
        chunk, so the "no longer current" half finds nothing. The interface
        promises it anyway, for a store with no foreign keys.
        """
        replaced = [record.chunk_id for record in records]
        self._db.execute(
            delete(ChunkEmbedding).where(
                ChunkEmbedding.service_id == service_id,
                or_(
                    ChunkEmbedding.chunk_id.not_in(current_chunk_ids),
                    ChunkEmbedding.chunk_id.in_(replaced),
                ),
            )
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

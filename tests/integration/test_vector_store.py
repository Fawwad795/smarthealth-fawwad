"""Tests for the chunk_embeddings table and PgVectorStore (task 4.6).

Run against the real test database, because what is being tested is
Postgres itself: the vector(384) column, the CASCADE from content_chunks,
the unique chunk_id and the <=> operator search will rely on. Vectors are
written by hand, so no embedding provider is involved.
"""

from contextlib import nullcontext

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.ai.vector_store import PgVectorStore, VectorRecord
from app.models import ChunkEmbedding, ContentChunk, Department, Service
from app.models.chunk_embedding import EMBEDDING_DIMENSIONS
from app.models.enums import ContentSourceType
from app.temporal.activities import ChunkContentInput, PublishActivities


def _vector(*leading: float) -> list[float]:
    """A full-size vector: `leading`, then zeros."""
    return [*leading, *[0.0] * (EMBEDDING_DIMENSIONS - len(leading))]


def _chunk(db: Session, service: Service, index: int = 0) -> ContentChunk:
    chunk = ContentChunk(
        source_type=ContentSourceType.SERVICE,
        source_id=service.id,
        chunk_index=index,
        text=f"chunk {index}",
        token_count=2,
        text_hash="0" * 64,
    )
    db.add(chunk)
    db.flush()
    return chunk


def _record(chunk: ContentChunk, *leading: float) -> VectorRecord:
    return VectorRecord(
        chunk_id=chunk.id,
        embedding=_vector(*(leading or (1.0,))),
        model="test-model",
        department="Cardiology",
        specialties=["Cardiology"],
    )


def _rows(db: Session, service: Service) -> list[ChunkEmbedding]:
    """This service's vectors, read back from Postgres rather than from the
    objects the store just added to the session."""
    db.expire_all()
    return list(
        db.scalars(
            select(ChunkEmbedding)
            .where(ChunkEmbedding.service_id == service.id)
            .order_by(ChunkEmbedding.chunk_id)
        )
    )


@pytest.fixture()
def store(db_session: Session) -> PgVectorStore:
    return PgVectorStore(db_session)


@pytest.fixture()
def other_service(db_session: Session, department: Department) -> Service:
    s = Service(department_id=department.id, name="Holter Monitor")
    db_session.add(s)
    db_session.flush()
    return s


def test_replace_stores_the_vector_with_its_metadata(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    chunk = _chunk(db_session, service)

    store.replace_service_vectors(
        service.id,
        [
            VectorRecord(
                chunk_id=chunk.id,
                embedding=_vector(0.5, 0.25),
                model="test-model",
                department="Cardiology",
                specialties=["Cardiology", "Paediatric Cardiology"],
            )
        ],
    )

    [row] = _rows(db_session, service)
    assert row.chunk_id == chunk.id
    assert len(row.embedding) == EMBEDDING_DIMENSIONS
    assert [float(x) for x in row.embedding[:3]] == [0.5, 0.25, 0.0]
    assert row.model == "test-model"
    assert row.department == "Cardiology"
    assert row.specialties == ["Cardiology", "Paediatric Cardiology"]
    assert row.published is False


def test_replace_stores_vectors_unpublished_even_for_a_published_service(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    """Re-embedding hides a service's vectors until mark_published runs
    again, so search never mixes a finished publish with an unfinished one.
    """
    chunk = _chunk(db_session, service)
    store.replace_service_vectors(service.id, [_record(chunk)])
    store.set_published(service.id, True)

    store.replace_service_vectors(service.id, [_record(chunk)])

    [row] = _rows(db_session, service)
    assert row.published is False


def test_replacing_twice_leaves_one_vector_per_chunk(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    """A retried Activity runs the replace again; it must not duplicate."""
    chunk = _chunk(db_session, service)

    store.replace_service_vectors(service.id, [_record(chunk, 1.0)])
    store.replace_service_vectors(service.id, [_record(chunk, 0.0, 1.0)])

    [row] = _rows(db_session, service)
    assert [float(x) for x in row.embedding[:2]] == [0.0, 1.0]


def test_replace_drops_the_vector_of_a_chunk_no_longer_given(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    kept, dropped = _chunk(db_session, service, 0), _chunk(db_session, service, 1)
    store.replace_service_vectors(service.id, [_record(kept), _record(dropped)])

    store.replace_service_vectors(service.id, [_record(kept)])

    assert [row.chunk_id for row in _rows(db_session, service)] == [kept.id]


def test_replace_leaves_other_services_vectors_alone(
    store: PgVectorStore,
    db_session: Session,
    service: Service,
    other_service: Service,
) -> None:
    other_chunk = _chunk(db_session, other_service)
    store.replace_service_vectors(other_service.id, [_record(other_chunk)])

    store.replace_service_vectors(service.id, [_record(_chunk(db_session, service))])

    assert len(_rows(db_session, other_service)) == 1


def test_set_published_flags_only_that_services_vectors(
    store: PgVectorStore,
    db_session: Session,
    service: Service,
    other_service: Service,
) -> None:
    store.replace_service_vectors(service.id, [_record(_chunk(db_session, service))])
    store.replace_service_vectors(
        other_service.id, [_record(_chunk(db_session, other_service))]
    )

    store.set_published(service.id, True)

    assert [row.published for row in _rows(db_session, service)] == [True]
    assert [row.published for row in _rows(db_session, other_service)] == [False]


def test_rechunking_a_service_deletes_its_old_vector_with_the_chunk(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    """chunk_content deletes and re-inserts a service's chunks on every
    publish. The CASCADE on chunk_id must take the old vector with it, or
    every re-publish would leave an orphan behind.
    """
    activities = PublishActivities(session_factory=lambda: nullcontext(db_session))
    activities.chunk_content(ChunkContentInput(service.id, "first text"))
    [old_chunk] = db_session.scalars(
        select(ContentChunk).where(ContentChunk.source_id == service.id)
    ).all()
    store.replace_service_vectors(service.id, [_record(old_chunk)])

    activities.chunk_content(ChunkContentInput(service.id, "second text"))

    assert _rows(db_session, service) == []


def test_a_vector_of_the_wrong_size_is_rejected(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    """Postgres enforces the column's size, so a provider configured for a
    different model cannot quietly store vectors that compare as nonsense.
    """
    chunk = _chunk(db_session, service)
    short = VectorRecord(
        chunk_id=chunk.id,
        embedding=[1.0, 0.0, 0.0],
        model="test-model",
        department="Cardiology",
        specialties=[],
    )

    with pytest.raises(DataError, match="expected 384 dimensions, not 3"):
        store.replace_service_vectors(service.id, [short])


def test_a_chunk_cannot_have_two_vectors(db_session: Session, service: Service) -> None:
    chunk = _chunk(db_session, service)
    for _ in range(2):
        db_session.add(
            ChunkEmbedding(
                chunk_id=chunk.id,
                service_id=service.id,
                embedding=_vector(1.0),
                model="test-model",
                department="Cardiology",
                specialties=[],
                published=False,
            )
        )

    with pytest.raises(IntegrityError, match="uq_chunk_embeddings_chunk_id"):
        db_session.flush()


def test_stored_vectors_rank_by_cosine_distance(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    """The column works with the <=> operator search (task 4.8) will order
    by: nearest direction first, whatever the vector's length."""
    chunks = [_chunk(db_session, service, i) for i in range(3)]
    store.replace_service_vectors(
        service.id,
        [
            _record(chunks[0], 0.0, 1.0),  # at right angles to the query
            _record(chunks[1], 5.0, 0.4),  # long, but pointing the query's way
            _record(chunks[2], 0.7, 0.7),  # halfway between
        ],
    )

    ranked = db_session.scalars(
        select(ChunkEmbedding.chunk_id).order_by(
            ChunkEmbedding.embedding.cosine_distance(_vector(1.0, 0.1))
        )
    ).all()

    assert ranked == [chunks[1].id, chunks[2].id, chunks[0].id]

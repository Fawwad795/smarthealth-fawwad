"""Tests for the chunk_embeddings table and PgVectorStore (tasks 4.6, 4.9).

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


def _record(
    chunk: ContentChunk, *leading: float, model: str = "test-model"
) -> VectorRecord:
    return VectorRecord(
        chunk_id=chunk.id,
        embedding=_vector(*(leading or (1.0,))),
        model=model,
        department="Cardiology",
        specialties=["Cardiology"],
    )


def _store_all(store: PgVectorStore, service: Service, *records: VectorRecord) -> None:
    """Store `records` as the service's complete set of current chunks."""
    store.store_service_vectors(
        service.id, list(records), [record.chunk_id for record in records]
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


def test_store_saves_the_vector_with_its_metadata(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    chunk = _chunk(db_session, service)

    _store_all(
        store,
        service,
        VectorRecord(
            chunk_id=chunk.id,
            embedding=_vector(0.5, 0.25),
            model="test-model",
            department="Cardiology",
            specialties=["Cardiology", "Paediatric Cardiology"],
        ),
    )

    [row] = _rows(db_session, service)
    assert row.chunk_id == chunk.id
    assert len(row.embedding) == EMBEDDING_DIMENSIONS
    assert [float(x) for x in row.embedding[:3]] == [0.5, 0.25, 0.0]
    assert row.model == "test-model"
    assert row.department == "Cardiology"
    assert row.specialties == ["Cardiology", "Paediatric Cardiology"]
    assert row.published is False


def test_store_saves_a_replacement_vector_unpublished(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    """Re-embedding a chunk of a published service hides the new vector
    until mark_published runs again, so search never returns a vector from
    an unfinished publish.
    """
    chunk = _chunk(db_session, service)
    _store_all(store, service, _record(chunk))
    store.set_published(service.id, True)

    _store_all(store, service, _record(chunk))

    [row] = _rows(db_session, service)
    assert row.published is False


def test_storing_twice_leaves_one_vector_per_chunk(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    """A retried Activity runs the store again; it must not duplicate."""
    chunk = _chunk(db_session, service)

    _store_all(store, service, _record(chunk, 1.0))
    _store_all(store, service, _record(chunk, 0.0, 1.0))

    [row] = _rows(db_session, service)
    assert [float(x) for x in row.embedding[:2]] == [0.0, 1.0]


def test_store_keeps_the_vector_of_a_current_chunk_it_was_not_given(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    """Task 4.9: an unchanged chunk is not re-embedded, so its vector must
    survive a store call that only carries the changed chunk's vector.
    The same row, not an equal copy: its id and published flag are kept.
    """
    unchanged, changed = _chunk(db_session, service, 0), _chunk(db_session, service, 1)
    _store_all(store, service, _record(unchanged), _record(changed))
    store.set_published(service.id, True)
    [kept_before, _] = _rows(db_session, service)

    store.store_service_vectors(
        service.id, [_record(changed, 0.0, 1.0)], [unchanged.id, changed.id]
    )

    [kept, replaced] = _rows(db_session, service)
    assert kept.id == kept_before.id
    assert kept.published is True
    assert [float(x) for x in replaced.embedding[:2]] == [0.0, 1.0]
    assert replaced.published is False


def test_store_drops_the_vector_of_a_chunk_no_longer_current(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    kept, dropped = _chunk(db_session, service, 0), _chunk(db_session, service, 1)
    _store_all(store, service, _record(kept), _record(dropped))

    store.store_service_vectors(service.id, [], [kept.id])

    assert [row.chunk_id for row in _rows(db_session, service)] == [kept.id]


def test_store_leaves_other_services_vectors_alone(
    store: PgVectorStore,
    db_session: Session,
    service: Service,
    other_service: Service,
) -> None:
    """current_chunk_ids names only this service's chunks; the other
    service's vector is not "no longer current" and must survive."""
    _store_all(store, other_service, _record(_chunk(db_session, other_service)))

    _store_all(store, service, _record(_chunk(db_session, service)))

    assert len(_rows(db_session, other_service)) == 1


def test_embedded_chunk_ids_counts_only_this_services_vectors_from_that_model(
    store: PgVectorStore,
    db_session: Session,
    service: Service,
    other_service: Service,
) -> None:
    """A vector from another model is no use to a new query, so it does not
    count as embedded: switching models must re-embed every chunk."""
    current, old_model = _chunk(db_session, service, 0), _chunk(db_session, service, 1)
    _store_all(
        store,
        service,
        _record(current, model="model-a"),
        _record(old_model, model="model-b"),
    )
    _store_all(
        store,
        other_service,
        _record(_chunk(db_session, other_service), model="model-a"),
    )

    assert store.embedded_chunk_ids(service.id, "model-a") == {current.id}


def test_set_published_flags_only_that_services_vectors(
    store: PgVectorStore,
    db_session: Session,
    service: Service,
    other_service: Service,
) -> None:
    _store_all(store, service, _record(_chunk(db_session, service)))
    _store_all(store, other_service, _record(_chunk(db_session, other_service)))

    store.set_published(service.id, True)

    assert [row.published for row in _rows(db_session, service)] == [True]
    assert [row.published for row in _rows(db_session, other_service)] == [False]


def test_rechunking_changed_text_deletes_the_old_vector_with_the_chunk(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    """chunk_content deletes a chunk whose text has changed. The CASCADE on
    chunk_id must take the old vector with it, or every re-publish of an
    edited service would leave an orphan behind.
    """
    activities = PublishActivities(session_factory=lambda: nullcontext(db_session))
    activities.chunk_content(ChunkContentInput(service.id, "first text"))
    [old_chunk] = db_session.scalars(
        select(ContentChunk).where(ContentChunk.source_id == service.id)
    ).all()
    _store_all(store, service, _record(old_chunk))

    activities.chunk_content(ChunkContentInput(service.id, "second text"))

    assert _rows(db_session, service) == []


def test_rechunking_unchanged_text_keeps_the_vector(
    store: PgVectorStore, db_session: Session, service: Service
) -> None:
    """The other half of the above, and the point of task 4.9: the same
    text keeps its chunk row, so the CASCADE never fires."""
    activities = PublishActivities(session_factory=lambda: nullcontext(db_session))
    activities.chunk_content(ChunkContentInput(service.id, "same text"))
    [chunk] = db_session.scalars(
        select(ContentChunk).where(ContentChunk.source_id == service.id)
    ).all()
    _store_all(store, service, _record(chunk))
    [before] = _rows(db_session, service)

    activities.chunk_content(ChunkContentInput(service.id, "same text"))

    [after] = _rows(db_session, service)
    assert after.id == before.id


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
        _store_all(store, service, short)


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
    _store_all(
        store,
        service,
        _record(chunks[0], 0.0, 1.0),  # at right angles to the query
        _record(chunks[1], 5.0, 0.4),  # long, but pointing the query's way
        _record(chunks[2], 0.7, 0.7),  # halfway between
    )

    ranked = db_session.scalars(
        select(ChunkEmbedding.chunk_id).order_by(
            ChunkEmbedding.embedding.cosine_distance(_vector(1.0, 0.1))
        )
    ).all()

    assert ranked == [chunks[1].id, chunks[2].id, chunks[0].id]

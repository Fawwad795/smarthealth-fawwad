"""Tests for the publish Activities, called directly as plain methods --
no Temporal server involved. PublishActivities is constructed with a
factory that hands back this test's own db_session (wrapped in
nullcontext so the Activity's `with ... as db:` doesn't close it), so
every write here lands in the same rolled-back transaction as the rest of
the suite.
"""

from contextlib import nullcontext

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError

from app.ai import embeddings
from app.ai.embeddings import (
    EmbeddingProvider,
    FakeEmbeddings,
    PermanentEmbeddingError,
    TransientEmbeddingError,
)
from app.ai.vector_store import PgVectorStore, VectorRecord
from app.models import (
    ChunkEmbedding,
    ContentChunk,
    Department,
    Provider,
    ProviderService,
    Service,
    Specialty,
    User,
)
from app.models.chunk_embedding import EMBEDDING_DIMENSIONS
from app.models.enums import ContentSourceType, ServiceStatus, UserRole
from app.temporal.activities import (
    ChunkContentInput,
    PublishActivities,
    PublishFailedInput,
    ServiceInput,
)


def _service(
    db_session: Session,
    department: Department,
    *,
    description: str | None = "Non-invasive imaging of the heart.",
    prep_instructions: str | None = "Arrive 15 minutes early.",
) -> Service:
    s = Service(
        department_id=department.id,
        name="Echocardiogram",
        description=description,
        prep_instructions=prep_instructions,
    )
    db_session.add(s)
    db_session.flush()
    return s


def _specialty(db_session: Session, name: str) -> Specialty:
    s = Specialty(name=name)
    db_session.add(s)
    db_session.flush()
    return s


def _provider(
    db_session: Session, department: Department, specialty: Specialty, email: str
) -> Provider:
    """A provider needs its own user row, so the shared `provider` fixture
    can't be reused when a test needs several."""
    user = User(email=email, password_hash="not-a-real-hash", role=UserRole.PROVIDER)
    db_session.add(user)
    db_session.flush()
    p = Provider(
        user_id=user.id, department_id=department.id, specialty_id=specialty.id
    )
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture()
def fake_embeddings() -> FakeEmbeddings:
    return FakeEmbeddings()


@pytest.fixture()
def activities(
    db_session: Session, fake_embeddings: FakeEmbeddings
) -> PublishActivities:
    return PublishActivities(
        session_factory=lambda: nullcontext(db_session),
        embeddings_factory=lambda: fake_embeddings,
    )


def test_validate_service_passes_when_complete(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    service = _service(db_session, department)
    activities.validate_service(ServiceInput(service.id))


def test_validate_service_raises_non_retryable_error_listing_missing_fields(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    service = _service(db_session, department, description=None, prep_instructions=None)

    with pytest.raises(ApplicationError) as exc_info:
        activities.validate_service(ServiceInput(service.id))

    assert exc_info.value.non_retryable is True
    assert "description" in str(exc_info.value)
    assert "prep_instructions" in str(exc_info.value)


def test_structure_content_lists_each_linked_providers_specialty_once(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    """Specialty reaches a service only through provider_services. Two
    linked providers share one specialty, and a provider in the same
    department who is not linked must contribute nothing."""
    service = _service(db_session, department)
    paediatric = _specialty(db_session, "Paediatric Cardiology")
    interventional = _specialty(db_session, "Interventional Cardiology")
    for email, specialty in [
        ("dr.one@example.com", paediatric),
        ("dr.two@example.com", interventional),
        ("dr.three@example.com", paediatric),
    ]:
        provider = _provider(db_session, department, specialty, email)
        db_session.add(ProviderService(provider_id=provider.id, service_id=service.id))
    _provider(
        db_session,
        department,
        _specialty(db_session, "Dermatology"),
        "dr.four@example.com",
    )
    db_session.flush()

    text = activities.structure_content(ServiceInput(service.id))

    assert text == (
        "Cardiology · Interventional Cardiology, Paediatric Cardiology: "
        "Echocardiogram. Non-invasive imaging of the heart. "
        "Preparation: Arrive 15 minutes early."
    )


def test_structure_content_without_linked_providers_has_no_specialty(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    """A service can be published before anyone is linked to deliver it."""
    service = _service(db_session, department)

    text = activities.structure_content(ServiceInput(service.id))

    assert text == (
        "Cardiology: Echocardiogram. Non-invasive imaging of the heart. "
        "Preparation: Arrive 15 minutes early."
    )


def test_chunk_content_writes_one_chunk(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    service = _service(db_session, department)

    written = activities.chunk_content(ChunkContentInput(service.id, "structured text"))

    assert written == 1
    chunk = db_session.execute(
        select(ContentChunk).where(
            ContentChunk.source_type == ContentSourceType.SERVICE,
            ContentChunk.source_id == service.id,
        )
    ).scalar_one()
    assert chunk.text == "structured text"
    assert chunk.chunk_index == 0


def test_chunk_content_replaces_rather_than_duplicates(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    service = _service(db_session, department)
    activities.chunk_content(ChunkContentInput(service.id, "first version"))

    activities.chunk_content(ChunkContentInput(service.id, "second version"))

    chunks = (
        db_session.execute(
            select(ContentChunk).where(
                ContentChunk.source_type == ContentSourceType.SERVICE,
                ContentChunk.source_id == service.id,
            )
        )
        .scalars()
        .all()
    )
    assert len(chunks) == 1
    assert chunks[0].text == "second version"


def test_mark_published_sets_status_and_timestamp(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    service = _service(db_session, department)

    activities.mark_published(ServiceInput(service.id))

    db_session.refresh(service)
    assert service.status == ServiceStatus.PUBLISHED
    assert service.published_at is not None


def test_mark_published_makes_the_services_vectors_searchable(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    """Vectors are stored unpublished; only mark_published flips them, in
    the same transaction as the service's own status."""
    service = _service(db_session, department)
    activities.chunk_content(ChunkContentInput(service.id, "some text"))
    chunk = db_session.scalars(
        select(ContentChunk).where(ContentChunk.source_id == service.id)
    ).one()
    PgVectorStore(db_session).replace_service_vectors(
        service.id,
        [
            VectorRecord(
                chunk_id=chunk.id,
                embedding=[1.0] * EMBEDDING_DIMENSIONS,
                model="test-model",
                department=department.name,
                specialties=[],
            )
        ],
    )

    activities.mark_published(ServiceInput(service.id))

    db_session.expire_all()
    embedding = db_session.scalars(
        select(ChunkEmbedding).where(ChunkEmbedding.service_id == service.id)
    ).one()
    assert embedding.published is True


def test_mark_publish_failed_sets_the_failed_status_and_reason(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    """The Workflow's clean-failure path after validate_service or
    embed_chunks gives up. The reason is what publish-status shows."""
    service = _service(db_session, department, description=None)
    service.status = ServiceStatus.PUBLISHING
    db_session.flush()

    activities.mark_publish_failed(
        PublishFailedInput(service.id, "description is required")
    )

    db_session.refresh(service)
    assert service.status == ServiceStatus.PUBLISH_FAILED
    assert service.publish_error == "description is required"


class _FailingEmbeddings(EmbeddingProvider):
    """A provider whose every call raises `error`."""

    def __init__(self, error: Exception) -> None:
        super().__init__(model="failing", dimensions=EMBEDDING_DIMENSIONS)
        self._error = error

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise self._error


def _activities_with(
    db_session: Session, provider: EmbeddingProvider
) -> PublishActivities:
    return PublishActivities(
        session_factory=lambda: nullcontext(db_session),
        embeddings_factory=lambda: provider,
    )


def _vectors(db_session: Session, service: Service) -> list[ChunkEmbedding]:
    """The service's stored vectors, read back from Postgres."""
    db_session.expire_all()
    return list(
        db_session.scalars(
            select(ChunkEmbedding)
            .where(ChunkEmbedding.service_id == service.id)
            .order_by(ChunkEmbedding.chunk_id)
        )
    )


def test_embed_chunks_stores_an_unpublished_vector_with_the_services_metadata(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    service = _service(db_session, department)
    for email, name in [
        ("dr.one@example.com", "Paediatric Cardiology"),
        ("dr.two@example.com", "Interventional Cardiology"),
    ]:
        provider = _provider(
            db_session, department, _specialty(db_session, name), email
        )
        db_session.add(ProviderService(provider_id=provider.id, service_id=service.id))
    activities.chunk_content(ChunkContentInput(service.id, "some text"))

    stored = activities.embed_chunks(ServiceInput(service.id))

    assert stored == 1
    [vector] = _vectors(db_session, service)
    assert [float(x) for x in vector.embedding] == pytest.approx(
        FakeEmbeddings().embed(["some text"])[0]
    )
    assert vector.model == "fake-hashing-v1"
    assert vector.department == department.name
    assert vector.specialties == ["Interventional Cardiology", "Paediatric Cardiology"]
    assert vector.published is False
    chunk = db_session.get(ContentChunk, vector.chunk_id)
    assert chunk.embedded_at is not None


def test_embed_chunks_batches_every_chunk_and_stores_each_vector_on_its_own_chunk(
    activities: PublishActivities,
    fake_embeddings: FakeEmbeddings,
    db_session: Session,
    department: Department,
) -> None:
    """A service's chunks go to the provider in one call, never one request
    per chunk, and each returned vector is stored against the chunk whose
    text produced it."""
    service = _service(db_session, department)
    for index in range(3):
        db_session.add(
            ContentChunk(
                source_type=ContentSourceType.SERVICE,
                source_id=service.id,
                chunk_index=index,
                text=f"part {index}",
                token_count=2,
                text_hash="0" * 64,
            )
        )
    db_session.flush()

    activities.embed_chunks(ServiceInput(service.id))

    [batch] = fake_embeddings.calls
    assert sorted(batch) == ["part 0", "part 1", "part 2"]
    vectors = _vectors(db_session, service)
    assert len(vectors) == 3
    for vector in vectors:
        chunk_text = db_session.get(ContentChunk, vector.chunk_id).text
        assert [float(x) for x in vector.embedding] == pytest.approx(
            FakeEmbeddings().embed([chunk_text])[0]
        )


def test_embed_chunks_twice_stores_one_vector_per_chunk(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    """Temporal retries an Activity whose result it never received, so a
    second run must not add a second vector."""
    service = _service(db_session, department)
    activities.chunk_content(ChunkContentInput(service.id, "some text"))

    activities.embed_chunks(ServiceInput(service.id))
    activities.embed_chunks(ServiceInput(service.id))

    assert len(_vectors(db_session, service)) == 1


def test_embed_chunks_raises_a_permanent_failure_as_non_retryable(
    db_session: Session, department: Department
) -> None:
    service = _service(db_session, department)
    failing = _activities_with(
        db_session,
        _FailingEmbeddings(PermanentEmbeddingError("Hugging Face returned HTTP 401")),
    )
    failing.chunk_content(ChunkContentInput(service.id, "some text"))

    with pytest.raises(ApplicationError, match="HTTP 401") as excinfo:
        failing.embed_chunks(ServiceInput(service.id))

    assert excinfo.value.non_retryable
    assert excinfo.value.type == "EMBEDDING_FAILED"
    assert _vectors(db_session, service) == []


def test_embed_chunks_raises_a_transient_failure_retryable_without_its_chain(
    db_session: Session, department: Department
) -> None:
    """Retryable, so the workflow's retry policy tries again. And without
    the httpx exception underneath: found live, Temporal replaced a
    retrying Activity's chained failure with "Failure exceeds size limit."
    in publish-status. Checked through the same converter Temporal uses.
    """
    error = TransientEmbeddingError("Hugging Face unreachable (ConnectTimeout)")
    error.__cause__ = httpx.ConnectTimeout("timed out")
    service = _service(db_session, department)
    failing = _activities_with(db_session, _FailingEmbeddings(error))
    failing.chunk_content(ChunkContentInput(service.id, "some text"))

    with pytest.raises(ApplicationError, match="ConnectTimeout") as excinfo:
        failing.embed_chunks(ServiceInput(service.id))

    assert not excinfo.value.non_retryable
    assert excinfo.value.type == "EMBEDDING_UNAVAILABLE"
    failure = Failure()
    DataConverter.default.failure_converter.to_failure(
        excinfo.value, DataConverter.default.payload_converter, failure
    )
    assert not failure.HasField("cause")


def test_embed_chunks_fails_permanently_without_an_api_key(
    db_session: Session, department: Department, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real factory, with no key: the worker still starts, and only the
    publish fails, once, with the provider's own message."""
    monkeypatch.setattr(embeddings.settings, "embedding_provider", "huggingface")
    monkeypatch.setattr(embeddings.settings, "embedding_api_key", SecretStr(""))
    service = _service(db_session, department)
    activities = PublishActivities(session_factory=lambda: nullcontext(db_session))
    activities.chunk_content(ChunkContentInput(service.id, "some text"))

    with pytest.raises(ApplicationError, match="EMBEDDING_API_KEY is empty") as excinfo:
        activities.embed_chunks(ServiceInput(service.id))

    assert excinfo.value.non_retryable


def test_embed_chunks_rejects_a_provider_of_the_wrong_size_before_calling_it(
    db_session: Session, department: Department
) -> None:
    """The column holds 384 numbers. Without this check the mismatch would
    surface as a database error, which Temporal retries every time."""
    service = _service(db_session, department)
    small = FakeEmbeddings(dimensions=16)
    activities = _activities_with(db_session, small)
    activities.chunk_content(ChunkContentInput(service.id, "some text"))

    with pytest.raises(ApplicationError, match="16-number vectors") as excinfo:
        activities.embed_chunks(ServiceInput(service.id))

    assert excinfo.value.non_retryable
    assert small.calls == []

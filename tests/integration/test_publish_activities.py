"""Tests for the publish Activities, called directly as plain methods --
no Temporal server involved. PublishActivities is constructed with a
factory that hands back this test's own db_session (wrapped in
nullcontext so the Activity's `with ... as db:` doesn't close it), so
every write here lands in the same rolled-back transaction as the rest of
the suite.
"""

from contextlib import nullcontext

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from temporalio.exceptions import ApplicationError

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
def activities(db_session: Session) -> PublishActivities:
    return PublishActivities(session_factory=lambda: nullcontext(db_session))


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


def test_mark_publish_failed_sets_the_failed_status(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    """The Workflow's clean-failure path after validate_service raises a
    non-retryable rejection -- reached only on that branch, so nothing
    else in the suite touches it.
    """
    service = _service(db_session, department, description=None)
    service.status = ServiceStatus.PUBLISHING
    db_session.flush()

    activities.mark_publish_failed(ServiceInput(service.id))

    db_session.refresh(service)
    assert service.status == ServiceStatus.PUBLISH_FAILED

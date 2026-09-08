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

from app.models import ContentChunk, Department, Service
from app.models.enums import ContentSourceType, ServiceStatus
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


def test_structure_content_combines_department_and_service_fields(
    activities: PublishActivities, db_session: Session, department: Department
) -> None:
    service = _service(db_session, department)

    text = activities.structure_content(ServiceInput(service.id))

    assert department.name in text
    assert service.name in text
    assert service.description in text
    assert service.prep_instructions in text


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

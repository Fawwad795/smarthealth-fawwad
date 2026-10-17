"""Re-publishing re-indexes a service (task 4.7), through the real publish
workflow and Activities on Temporal's test server.

The Definition of Done asks for this to be demonstrated: re-publishing an
edited service leaves exactly one vector, built from the new text, and no
stale one. These tests publish, change the service, publish again, and
read chunk_embeddings back.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from temporalio.client import Client

from app.ai.embeddings import FakeEmbeddings
from app.ai.retrieval import search_services
from app.core.config import settings
from app.models import ChunkEmbedding, ContentChunk, Department, Service
from app.models.enums import ContentSourceType, ServiceStatus
from app.schemas.service import ServiceUpdate
from app.services import service_publish
from app.services.service import update_service


def _service(db_session: Session, department: Department) -> Service:
    s = Service(
        department_id=department.id,
        name="Knee X-Ray",
        description="Imaging of the knee joint.",
        prep_instructions="Wear loose clothing.",
    )
    db_session.add(s)
    db_session.flush()
    return s


async def _publish(db_session: Session, client: Client, service: Service) -> None:
    """Start the real workflow the way the API does, and wait for it."""
    _, workflow_id = await service_publish.start_publish(db_session, service.id)
    await client.get_workflow_handle(workflow_id).result()
    db_session.expire_all()


def _vectors(db_session: Session, service: Service) -> list[ChunkEmbedding]:
    db_session.expire_all()
    return list(
        db_session.scalars(
            select(ChunkEmbedding).where(ChunkEmbedding.service_id == service.id)
        )
    )


def _chunk(db_session: Session, service: Service) -> ContentChunk:
    return db_session.scalars(
        select(ContentChunk).where(
            ContentChunk.source_type == ContentSourceType.SERVICE,
            ContentChunk.source_id == service.id,
        )
    ).one()


async def test_republishing_an_edited_service_leaves_one_vector_of_the_new_text(
    db_session: Session, department: Department, publishing_client: Client
) -> None:
    service = _service(db_session, department)
    await _publish(db_session, publishing_client, service)
    [old_vector] = _vectors(db_session, service)
    old_vector_id = old_vector.id

    update_service(
        db_session,
        service.id,
        ServiceUpdate(description="Imaging of the knee and lower leg."),
    )
    await _publish(db_session, publishing_client, service)

    assert service.status == ServiceStatus.PUBLISHED
    [vector] = _vectors(db_session, service)
    assert vector.id != old_vector_id
    assert vector.chunk_id == _chunk(db_session, service).id
    assert "knee and lower leg" in _chunk(db_session, service).text
    assert vector.published is True
    assert db_session.get(ChunkEmbedding, old_vector_id) is None


def _found(db_session: Session, query: str) -> list[int]:
    """The ids search returns for `query`, embedded the way the
    publishing_client fixture embedded the services."""
    return [
        result.service_id
        for result in search_services(db_session, query, 5, FakeEmbeddings())
    ]


async def test_wording_removed_by_a_republish_no_longer_finds_the_service(
    db_session: Session,
    department: Department,
    publishing_client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-indexing seen from search. A stale vector would go on matching
    the old wording, so a word only the old text had must stop finding the
    service once the edited text is published."""
    monkeypatch.setattr(settings, "retrieval_min_similarity", 0.1)
    service = _service(db_session, department)
    service.prep_instructions = "Bring the orange form."
    db_session.flush()
    await _publish(db_session, publishing_client, service)
    assert _found(db_session, "orange") == [service.id]

    update_service(
        db_session,
        service.id,
        ServiceUpdate(prep_instructions="Bring the purple form."),
    )
    await _publish(db_session, publishing_client, service)

    assert _found(db_session, "orange") == []
    assert _found(db_session, "purple") == [service.id]


async def test_republishing_an_unchanged_service_keeps_its_vector_searchable(
    db_session: Session, department: Department, publishing_client: Client
) -> None:
    """The vector is unpublished when the re-publish starts and must be
    published again when it finishes, even though nothing re-embedded it."""
    service = _service(db_session, department)
    await _publish(db_session, publishing_client, service)
    [before] = _vectors(db_session, service)

    await _publish(db_session, publishing_client, service)

    [after] = _vectors(db_session, service)
    assert after.id == before.id
    assert after.published is True


async def test_publishing_an_unpublished_service_again_makes_it_searchable(
    db_session: Session, department: Department, publishing_client: Client
) -> None:
    service = _service(db_session, department)
    await _publish(db_session, publishing_client, service)
    service_publish.unpublish_service(db_session, service.id)
    assert [v.published for v in _vectors(db_session, service)] == [False]

    await _publish(db_session, publishing_client, service)

    assert service.status == ServiceStatus.PUBLISHED
    assert [v.published for v in _vectors(db_session, service)] == [True]


async def test_a_failed_republish_leaves_the_service_out_of_search(
    db_session: Session, department: Department, publishing_client: Client
) -> None:
    """The known cost of going straight from PUBLISHED to PUBLISHING: a
    re-publish that fails takes the service out of search until a publish
    succeeds. Its status says so, and so does every vector's flag."""
    service = _service(db_session, department)
    await _publish(db_session, publishing_client, service)
    # ServiceUpdate cannot clear a field (None means "leave it alone"), so
    # the row is edited directly to make validate_service fail.
    service.prep_instructions = None
    db_session.flush()

    await _publish(db_session, publishing_client, service)

    assert service.status == ServiceStatus.PUBLISH_FAILED
    assert [v.published for v in _vectors(db_session, service)] == [False]

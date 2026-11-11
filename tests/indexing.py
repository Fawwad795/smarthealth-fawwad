"""Index a service by hand, the way a finished publish leaves it.

Shared by the tests that search (POST /search, POST /assistant/ask). The
publish workflow itself is covered by test_service_reindex.py; these tests
only need a service and its vector in place.
"""

from sqlalchemy.orm import Session

from app.ai.embeddings import FakeEmbeddings
from app.ai.vector_store import PgVectorStore, VectorRecord
from app.models import ContentChunk, Department, Service
from app.models.enums import ContentSourceType, ServiceStatus


def index_service(
    db: Session,
    department: Department,
    name: str,
    text: str,
    *,
    status: ServiceStatus = ServiceStatus.PUBLISHED,
    description: str | None = None,
    prep_instructions: str | None = None,
) -> Service:
    """A service with one chunk of `text` and its FakeEmbeddings vector.

    The vector is published only if the service is, as the publish workflow
    leaves it. `text` is what search compares against; `description` and
    `prep_instructions` are what the assistant shows the model.
    """
    service = Service(
        department_id=department.id,
        name=name,
        status=status,
        description=description,
        prep_instructions=prep_instructions,
    )
    db.add(service)
    db.flush()
    chunk = ContentChunk(
        source_type=ContentSourceType.SERVICE,
        source_id=service.id,
        chunk_index=0,
        text=text,
        token_count=len(text) // 4,
        text_hash="0" * 64,
    )
    db.add(chunk)
    db.flush()
    fake = FakeEmbeddings()
    store = PgVectorStore(db)
    store.store_service_vectors(
        service.id,
        [
            VectorRecord(
                chunk_id=chunk.id,
                embedding=fake.embed([text])[0],
                model=fake.model,
                department=department.name,
                specialties=["Orthopedics"],
            )
        ],
        [chunk.id],
    )
    store.set_published(service.id, status == ServiceStatus.PUBLISHED)
    return service

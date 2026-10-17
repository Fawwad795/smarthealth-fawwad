"""The retrieval eval against the real vector store (task 4.10).

The eval must rank every published service with no threshold, or the sweep
would never see the low scores the threshold is chosen from. Vectors are
written by hand, and the queries are embedded by a stand-in that returns a
vector chosen by the test, so every similarity is exact.
"""

import pytest
from sqlalchemy.orm import Session

from app.ai.embeddings import EmbeddingProvider
from app.ai.vector_store import PgVectorStore, VectorRecord
from app.core.config import settings
from app.models import ContentChunk, Department, Service
from app.models.chunk_embedding import EMBEDDING_DIMENSIONS
from app.models.enums import ContentSourceType, ServiceStatus
from scripts.eval_retrieval import EvalCase, evaluate, published_services

MODEL = "test-model"


def _vector(*leading: float) -> list[float]:
    """A full-size vector: `leading`, then zeros."""
    return [*leading, *[0.0] * (EMBEDDING_DIMENSIONS - len(leading))]


class _QueryVectors(EmbeddingProvider):
    """Embeds each query as the vector the test gave for it, and records
    every batch it is sent."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        super().__init__(model=MODEL, dimensions=EMBEDDING_DIMENSIONS)
        self._vectors = vectors
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vectors[text] for text in texts]


def _indexed(
    db: Session,
    department: Department,
    name: str,
    *leading: float,
    status: ServiceStatus = ServiceStatus.PUBLISHED,
) -> Service:
    """A service with one chunk whose vector points along `leading`,
    published only if the service is, as the publish workflow leaves it."""
    service = Service(department_id=department.id, name=name, status=status)
    db.add(service)
    db.flush()
    chunk = ContentChunk(
        source_type=ContentSourceType.SERVICE,
        source_id=service.id,
        chunk_index=0,
        text=name,
        token_count=2,
        text_hash="0" * 64,
    )
    db.add(chunk)
    db.flush()
    store = PgVectorStore(db)
    store.store_service_vectors(
        service.id,
        [
            VectorRecord(
                chunk_id=chunk.id,
                embedding=_vector(*leading),
                model=MODEL,
                department=department.name,
                specialties=[],
            )
        ],
        [chunk.id],
    )
    store.set_published(service.id, status == ServiceStatus.PUBLISHED)
    return service


def test_published_services_leaves_out_a_service_that_is_not_published(
    db_session: Session, department: Department
) -> None:
    knee = _indexed(db_session, department, "Knee X-Ray", 1.0)
    _indexed(db_session, department, "Draft", 1.0, status=ServiceStatus.DRAFT)

    assert published_services(db_session) == {knee.id: "Knee X-Ray"}


def test_evaluate_ranks_every_published_service_whatever_its_score(
    db_session: Session, department: Department, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service at similarity 0, and one pointing the opposite way at -1,
    are still ranked, though both are far below the configured threshold,
    and all three are ranked though POST /search's top_k is 2 here. The
    unpublished service is not: the eval searches what POST /search
    searches."""
    monkeypatch.setattr(settings, "retrieval_min_similarity", 0.65)
    monkeypatch.setattr(settings, "retrieval_top_k", 2)
    _indexed(db_session, department, "Same", 1.0, 0.0)
    _indexed(db_session, department, "Unrelated", 0.0, 1.0)
    _indexed(db_session, department, "Opposite", -1.0, 0.0)
    _indexed(db_session, department, "Draft", 1.0, 0.0, status=ServiceStatus.DRAFT)
    embeddings = _QueryVectors({"a query": _vector(1.0, 0.0)})

    [result] = evaluate(
        db_session,
        embeddings,
        (EvalCase("a query", "Unrelated"),),
        published_services(db_session),
    )

    assert [(name, round(score, 3)) for name, score in result.ranking] == [
        ("Same", 1.0),
        ("Unrelated", 0.0),
        ("Opposite", -1.0),
    ]
    assert result.rank == 2


def test_evaluate_embeds_every_query_in_one_batch(
    db_session: Session, department: Department
) -> None:
    _indexed(db_session, department, "Knee X-Ray", 1.0)
    embeddings = _QueryVectors({"first": _vector(1.0), "second": _vector(1.0)})

    evaluate(
        db_session,
        embeddings,
        (EvalCase("first", "Knee X-Ray"), EvalCase("second", None)),
        published_services(db_session),
    )

    assert embeddings.calls == [["first", "second"]]

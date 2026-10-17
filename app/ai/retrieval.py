"""Semantic search over the published services (task 4.8).

Embeds the query with the same provider that embedded the services, asks the
vector store for the nearest published services above the similarity
threshold, and adds each service's name. POST /search calls it today; the
Week 5 assistant will call it to find what to ground an answer in.

Every filter runs in the vector store's SQL query, never here and never in a
prompt: published only (a DRAFT, PUBLISHING, PUBLISH_FAILED or INACTIVE
service has no published vector), current model only, and the threshold. In
this project a service is offered exactly when it is PUBLISHED, the same
definition Week 1's catalogue search uses; INACTIVE is how the clinic
withdraws one.

PHI: the index holds only service text (app/ai/chunking.py builds it from
service, department and specialty fields), so service search has no patient
data to scope. The query itself is the caller's own words, so it is never
logged or stored; logs carry the result ids only.
"""

import logging
from dataclasses import dataclass

from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.embeddings import EmbeddingError, EmbeddingProvider, get_embedding_provider
from app.ai.vector_store import get_vector_store
from app.core.config import settings
from app.core.exceptions import AppError
from app.models import Service
from app.models.chunk_embedding import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievedService:
    """One search result: the service, its department and specialties as
    stored on its vector, and its cosine similarity to the query."""

    service_id: int
    name: str
    department: str
    specialties: list[str]
    similarity: float


def get_query_embeddings() -> EmbeddingProvider:
    """The embedding provider for a search request, as a FastAPI dependency.

    A dependency rather than a direct call so tests can swap in
    FakeEmbeddings through app.dependency_overrides. A provider that cannot
    be built (the hosted one with no key) is a 503, not a 500: search is
    unavailable, nothing is wrong with the request.
    """
    try:
        return get_embedding_provider()
    except EmbeddingError as err:
        raise _search_unavailable(err) from None


def search_services(
    db: Session, query: str, top_k: int, embeddings: EmbeddingProvider
) -> list[RetrievedService]:
    """The `top_k` published services most similar to `query`, most
    similar first, each at or above RETRIEVAL_MIN_SIMILARITY.

    An empty list is a valid answer: nothing the clinic offers is close
    enough, and the caller says so rather than showing weak matches.
    Raises a 503 AppError if the query cannot be embedded.
    """
    # Checked before embedding: the column holds vectors of this size, and
    # comparing a vector of another size is a database error, not an empty
    # result.
    if embeddings.dimensions != EMBEDDING_DIMENSIONS:
        raise _search_unavailable(
            EmbeddingError(
                f"the provider makes {embeddings.dimensions}-number vectors; "
                f"chunk_embeddings holds {EMBEDDING_DIMENSIONS}"
            )
        )
    try:
        [query_embedding] = embeddings.embed([query])
    except EmbeddingError as err:
        raise _search_unavailable(err) from None

    hits = get_vector_store(db).search(
        query_embedding,
        model=embeddings.model,
        top_k=top_k,
        min_similarity=settings.retrieval_min_similarity,
    )
    # The name is not stored on the vector, so it comes from services, in
    # one query for all the hits.
    names = dict(
        db.execute(
            select(Service.id, Service.name).where(
                Service.id.in_([hit.service_id for hit in hits])
            )
        ).all()
    )
    results = [
        RetrievedService(
            service_id=hit.service_id,
            name=names[hit.service_id],
            department=hit.department,
            specialties=hit.specialties,
            similarity=hit.similarity,
        )
        for hit in hits
    ]
    logger.info(
        "search returned %d services: %s",
        len(results),
        [result.service_id for result in results],
    )
    return results


def _search_unavailable(err: EmbeddingError) -> AppError:
    """The 503 for a query that could not be embedded. The provider's own
    message is logged, not returned: the client only needs to know search
    is down. Provider messages carry status codes and counts, never the
    query (app/ai/embeddings.py), so logging one is safe."""
    logger.warning("search unavailable: %s", err)
    return AppError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="SEARCH_UNAVAILABLE",
        message="Search is unavailable right now. Please try again shortly.",
    )

"""POST /search: semantic search over the published services (task 4.8).

Any signed-in user may search. The route parses the request, hands the
query to app.ai.retrieval and shapes the answer; the filtering and the
threshold live there.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.ai.embeddings import EmbeddingProvider
from app.ai.retrieval import get_query_embeddings, search_services
from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db.session import get_db
from app.schemas.errors import error_responses
from app.schemas.search import SearchRequest, SearchResponse, SearchResult

router = APIRouter(prefix="/search", tags=["search"])

NO_MATCH_MESSAGE = (
    "We don't offer a service that matches this search. "
    "Try describing it differently, or contact the front desk."
)


@router.post(
    "",
    response_model=SearchResponse,
    summary="Find services by meaning",
    responses=error_responses(
        {
            status.HTTP_401_UNAUTHORIZED: "Not signed in.",
            status.HTTP_503_SERVICE_UNAVAILABLE: "SEARCH_UNAVAILABLE -- the query could not be embedded.",
        }
    ),
)
def search(
    body: SearchRequest,
    db: Session = Depends(get_db),
    embeddings: EmbeddingProvider = Depends(get_query_embeddings),
    _current_user=Depends(get_current_user),
) -> SearchResponse:
    """Return the published services most similar to the query, with
    their department, specialties and similarity. Any signed-in user.

    200 with an empty list and a message when nothing is similar enough.
    401 without a valid token, 422 for an empty or over-long query or a
    top_k outside 1-20, 503 if the query cannot be embedded.

    A plain def, not async: embedding with the local model is CPU work
    that would block the event loop, so FastAPI runs this in its thread
    pool instead.
    """
    results = search_services(db, body.query, body.top_k, embeddings)
    return SearchResponse(
        results=[
            SearchResult(
                service_id=result.service_id,
                name=result.name,
                department=result.department,
                specialties=result.specialties,
                similarity=round(result.similarity, 3),
            )
            for result in results
        ],
        min_similarity=settings.retrieval_min_similarity,
        message=None if results else NO_MATCH_MESSAGE,
    )

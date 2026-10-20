"""POST /assistant/ask: the patient assistant (task 5.3).

Patients only. The route parses the question, hands it to
app.services.assistant and shapes the reply; the safety check, retrieval
and the answer live there.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.ai.embeddings import EmbeddingProvider
from app.ai.llm import LLMProvider
from app.ai.retrieval import get_query_embeddings
from app.core.dependencies import require_role
from app.db.session import get_db
from app.models import User
from app.models.enums import UserRole
from app.schemas.assistant import AskRequest, AskResponse, Citation
from app.schemas.errors import error_responses
from app.services import assistant as assistant_service
from app.services.assistant import get_assistant_llm

router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="Ask the patient assistant",
    responses=error_responses(
        {
            status.HTTP_401_UNAUTHORIZED: "Not signed in.",
            status.HTTP_403_FORBIDDEN: "Not a patient.",
            status.HTTP_503_SERVICE_UNAVAILABLE: (
                "ASSISTANT_UNAVAILABLE -- the model could not be reached; "
                "SEARCH_UNAVAILABLE -- the question could not be embedded."
            ),
        }
    ),
)
async def ask_assistant(
    body: AskRequest,
    current_user: User = Depends(require_role(UserRole.PATIENT)),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_assistant_llm),
    embeddings: EmbeddingProvider = Depends(get_query_embeddings),
) -> AskResponse:
    """Answer a patient's question about the clinic's services, citing the
    services the answer drew on. PATIENT only.

    200 for an answer, a refusal of medical advice, or "we don't offer
    that"; `outcome` says which. 401 without a valid token, 403 for staff,
    422 for an empty, over-long or letterless question, 503 if the model or
    search is unavailable.

    async, unlike POST /search: the model calls are awaited, so this request
    gives way to others while the model works. The service runs the
    blocking parts (embedding, the database) in worker threads.
    """
    answer = await assistant_service.ask(
        db,
        user_id=current_user.id,
        question=body.question,
        llm=llm,
        embeddings=embeddings,
    )
    return AskResponse(
        answer=answer.text,
        intent=answer.intent,
        outcome=answer.outcome,
        citations=[
            Citation(
                service_id=service.service_id,
                name=service.name,
                department=service.department,
                similarity=round(service.similarity, 3),
            )
            for service in answer.citations
        ],
        interaction_id=answer.interaction_id,
    )

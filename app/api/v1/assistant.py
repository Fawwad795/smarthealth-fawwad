"""POST /assistant/ask and /assistant/ask/stream: the patient assistant
(tasks 5.3 and 5.8).

Patients only. The routes parse the question, hand it to
app.services.assistant and shape the reply; the safety check, retrieval
and the answer live there. /ask returns the reply as one JSON body;
/ask/stream sends it as server-sent events while the model writes it.
"""

from collections.abc import AsyncGenerator
from contextlib import aclosing

from fastapi import APIRouter, Depends, status
from redis import Redis
from sqlalchemy.orm import Session

from app.ai.embeddings import EmbeddingProvider
from app.ai.llm import LLMProvider
from app.ai.retrieval import RetrievedService, get_query_embeddings
from app.core.dependencies import require_role
from app.core.redis import get_redis
from app.core.sse import SSEResponse, sse_event
from app.db.session import get_db
from app.models import User
from app.models.enums import InteractionOutcome, UserRole
from app.schemas.assistant import (
    AskRequest,
    AskResponse,
    Citation,
    StreamCitations,
    StreamDone,
    StreamToken,
)
from app.schemas.errors import ErrorDetail, ErrorResponse, error_responses
from app.services import assistant as assistant_service
from app.services.assistant import AssistantAnswer, get_assistant_llm
from app.services.rate_limit import limit_ai_requests

router = APIRouter(prefix="/assistant", tags=["assistant"])

# The failures both routes document. Each happens before a reply starts,
# so /ask/stream returns them as ordinary JSON errors too.
#
# Both routes declare the rate limit after the role check. FastAPI resolves
# dependencies in the order they are declared, so a request refused for its
# role is never counted against the limit.
ASK_ERRORS = {
    status.HTTP_401_UNAUTHORIZED: "Not signed in.",
    status.HTTP_403_FORBIDDEN: "Not a patient.",
    status.HTTP_429_TOO_MANY_REQUESTS: (
        "RATE_LIMITED -- over the AI requests allowed per minute; "
        "Retry-After says when to try again."
    ),
    status.HTTP_503_SERVICE_UNAVAILABLE: (
        "ASSISTANT_UNAVAILABLE -- the model could not be reached; "
        "SEARCH_UNAVAILABLE -- the question could not be embedded."
    ),
}


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="Ask the patient assistant",
    responses=error_responses(ASK_ERRORS),
)
async def ask_assistant(
    body: AskRequest,
    current_user: User = Depends(require_role(UserRole.PATIENT)),
    _rate_limit: None = Depends(limit_ai_requests),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_assistant_llm),
    embeddings: EmbeddingProvider = Depends(get_query_embeddings),
    redis_client: Redis = Depends(get_redis),
) -> AskResponse:
    """Answer a patient's question about the clinic's services, citing the
    services the answer drew on. PATIENT only.

    200 for an answer, a refusal of medical advice, or "we don't offer
    that"; `outcome` says which. 401 without a valid token, 403 for staff,
    429 over the AI rate limit,
    422 for an empty, over-long or letterless question, 503 if the model or
    search is unavailable.

    async, unlike POST /search: the model calls are awaited, so this request
    gives way to others while the model works. The service runs the
    blocking parts (embedding, the database, Redis) in worker threads.

    A question already answered about the same services may come from the
    answer cache, with no model call.
    """
    answer = await assistant_service.ask(
        db,
        user_id=current_user.id,
        question=body.question,
        llm=llm,
        embeddings=embeddings,
        redis_client=redis_client,
    )
    return AskResponse(
        answer=answer.text,
        intent=answer.intent,
        outcome=answer.outcome,
        citations=_citations(answer.citations),
        interaction_id=answer.interaction_id,
    )


@router.post(
    "/ask/stream",
    response_class=SSEResponse,
    summary="Ask the patient assistant, with the reply streamed",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "Server-sent events: `token` events carrying the reply as the "
                "model writes it, then `citations`, then `done`. If the model "
                "fails part-way, an `error` event replaces `citations`."
            ),
        },
        **error_responses(ASK_ERRORS),
    },
)
async def ask_assistant_stream(
    body: AskRequest,
    current_user: User = Depends(require_role(UserRole.PATIENT)),
    _rate_limit: None = Depends(limit_ai_requests),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_assistant_llm),
    embeddings: EmbeddingProvider = Depends(get_query_embeddings),
    redis_client: Redis = Depends(get_redis),
) -> SSEResponse:
    """Answer like POST /assistant/ask, but stream the reply as the model
    writes it. PATIENT only. Try it with `curl -N`.

    401, 403, 422, 429 and 503 as for /ask: retrieval and the safety check run
    before the stream starts, so their failures are still JSON errors. Once
    the 200 is sent, a model failure can only arrive as an `error` event.
    """
    answer = await assistant_service.ask_stream(
        db,
        user_id=current_user.id,
        question=body.question,
        llm=llm,
        embeddings=embeddings,
        redis_client=redis_client,
    )
    return SSEResponse(_events(answer))


async def _events(
    answer: AsyncGenerator[str | AssistantAnswer, None],
) -> AsyncGenerator[str, None]:
    """Turn the service's stream into server-sent events.

    Every stream ends with `done`. Before it comes `citations` when the
    reply was sent in full, or `error`, in the API's usual error shape,
    when the model failed part-way.

    aclosing() passes a disconnect on: when SSEResponse closes this
    generator, the service's generator is closed straight away too, so it
    records the question as INTERRUPTED and stops the model.
    """
    async with aclosing(answer) as items:
        async for item in items:
            if isinstance(item, str):
                yield sse_event("token", StreamToken(text=item))
                continue
            if item.outcome is InteractionOutcome.FAILED:
                error = ErrorDetail(
                    code=assistant_service.UNAVAILABLE_CODE,
                    message=assistant_service.UNAVAILABLE_MESSAGE,
                )
                yield sse_event("error", ErrorResponse(error=error))
            else:
                citations = StreamCitations(citations=_citations(item.citations))
                yield sse_event("citations", citations)
            yield sse_event(
                "done",
                StreamDone(
                    intent=item.intent,
                    outcome=item.outcome,
                    interaction_id=item.interaction_id,
                ),
            )


def _citations(services: list[RetrievedService]) -> list[Citation]:
    """The services a reply drew on, as the response lists them."""
    return [
        Citation(
            service_id=service.service_id,
            name=service.name,
            department=service.department,
            similarity=round(service.similarity, 3),
        )
        for service in services
    ]

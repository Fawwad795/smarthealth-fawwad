"""Staff generation routes (task 5.9): an appointment's summary and
follow-up draft, and the utilisation report. Front desk and admin only.

Each streams over SSE like /assistant/ask/stream. The routes parse the
request, hand it to app.services.generation and turn what it yields into
events.
"""

from collections.abc import AsyncGenerator
from contextlib import aclosing

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.ai.llm import LLMProvider
from app.core.dependencies import require_role
from app.core.sse import SSEResponse, sse_event
from app.db.session import get_db
from app.models import User
from app.models.enums import GenerationKind, UserRole
from app.schemas.analytics import DateRange
from app.schemas.assistant import StreamToken
from app.schemas.errors import ErrorDetail, ErrorResponse, error_responses
from app.schemas.generation import GenerationCitations, GenerationDone, GenerationRetry
from app.services import generation as generation_service
from app.services.generation import GenerationResult, ReportRetry, get_generation_llm
from app.services.rate_limit import limit_ai_requests

router = APIRouter(tags=["generation"])

_STAFF = (UserRole.FRONT_DESK, UserRole.ADMIN)

# Each route declares the rate limit after the role check. FastAPI resolves
# dependencies in the order they are declared, so a request refused for its
# role is never counted against the limit.
_ERRORS = {
    status.HTTP_401_UNAUTHORIZED: "Not signed in.",
    status.HTTP_403_FORBIDDEN: "Front desk and admin only.",
    status.HTTP_429_TOO_MANY_REQUESTS: (
        "RATE_LIMITED -- over the AI requests allowed per minute; "
        "Retry-After says when to try again."
    ),
    status.HTTP_503_SERVICE_UNAVAILABLE: "GENERATION_UNAVAILABLE -- no model is configured.",
}

_APPOINTMENT_ERRORS = {
    **_ERRORS,
    status.HTTP_404_NOT_FOUND: "APPOINTMENT_NOT_FOUND.",
}

_TEXT_STREAM = (
    "Server-sent events: `token` events carrying the text as the model writes "
    "it, then `citations`, then `done`. If the model fails part-way, an "
    "`error` event replaces `citations`."
)

_REPORT_STREAM = (
    "Server-sent events: `token` events carrying the model's JSON as it is "
    "written, a `retry` event and new tokens if the first reply could not be "
    "used, then `report` (the validated report) and `done`. If both replies "
    "fail validation, or the model fails, an `error` event replaces `report`."
)


@router.post(
    "/appointments/{appointment_id}/generate/summary",
    response_class=SSEResponse,
    summary="Generate a patient-facing summary of an appointment",
    responses={
        status.HTTP_200_OK: {"description": _TEXT_STREAM},
        **error_responses(_APPOINTMENT_ERRORS),
    },
)
async def generate_summary(
    appointment_id: int,
    current_user: User = Depends(require_role(*_STAFF)),
    _rate_limit: None = Depends(limit_ai_requests),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_generation_llm),
) -> SSEResponse:
    """Stream a short summary of the appointment, for staff to review
    before sending. FRONT_DESK/ADMIN only.

    401 without a token, 403 for other roles, 404 for an unknown
    appointment, 429 over the AI rate limit, 503 if no model is configured.
    """
    stream = await generation_service.generate_for_appointment(
        db,
        user_id=current_user.id,
        appointment_id=appointment_id,
        kind=GenerationKind.SUMMARY,
        llm=llm,
    )
    return SSEResponse(_events(stream))


@router.post(
    "/appointments/{appointment_id}/generate/followup",
    response_class=SSEResponse,
    summary="Generate a follow-up message draft for an appointment",
    responses={
        status.HTTP_200_OK: {"description": _TEXT_STREAM},
        **error_responses(_APPOINTMENT_ERRORS),
    },
)
async def generate_followup(
    appointment_id: int,
    current_user: User = Depends(require_role(*_STAFF)),
    _rate_limit: None = Depends(limit_ai_requests),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_generation_llm),
) -> SSEResponse:
    """Stream a follow-up message draft matched to the appointment's
    status, for staff to review before sending. FRONT_DESK/ADMIN only.

    401 without a token, 403 for other roles, 404 for an unknown
    appointment, 429 over the AI rate limit, 503 if no model is configured.
    """
    stream = await generation_service.generate_for_appointment(
        db,
        user_id=current_user.id,
        appointment_id=appointment_id,
        kind=GenerationKind.FOLLOWUP,
        llm=llm,
    )
    return SSEResponse(_events(stream))


@router.post(
    "/reports/generate/utilisation",
    response_class=SSEResponse,
    summary="Generate a utilisation report over a date range",
    responses={
        status.HTTP_200_OK: {"description": _REPORT_STREAM},
        **error_responses(_ERRORS),
    },
)
async def generate_utilisation_report(
    body: DateRange,
    current_user: User = Depends(require_role(*_STAFF)),
    _rate_limit: None = Depends(limit_ai_requests),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_generation_llm),
) -> SSEResponse:
    """Stream a utilisation report: the analytics figures for the range
    and each department's booked slots, with the model's commentary.
    FRONT_DESK/ADMIN only.

    The body takes `start_date` and `end_date`, both optional, with GET
    /analytics/summary's defaults (the last 30 days). 401 without a token,
    403 for other roles, 422 for start after end or a range over 366 days,
    429 over the AI rate limit,
    503 if no model is configured.
    """
    stream = await generation_service.generate_report(
        db,
        user_id=current_user.id,
        start_date=body.start_date,
        end_date=body.end_date,
        llm=llm,
    )
    return SSEResponse(_events(stream))


async def _events(
    stream: AsyncGenerator[str | ReportRetry | GenerationResult, None],
) -> AsyncGenerator[str, None]:
    """Turn the service's stream into server-sent events.

    Every stream ends with `done`. Before it comes `error` for a failed
    run, `report` for a report, or `citations` for a summary or follow-up.
    aclosing() passes a disconnect on to the service's generator, as in
    the assistant route.
    """
    async with aclosing(stream) as items:
        async for item in items:
            if isinstance(item, str):
                yield sse_event("token", StreamToken(text=item))
                continue
            if isinstance(item, ReportRetry):
                yield sse_event("retry", GenerationRetry(reason=item.problem))
                continue
            if item.error_code is not None:
                error = ErrorDetail(code=item.error_code, message=item.error_message)
                yield sse_event("error", ErrorResponse(error=error))
            elif item.report is not None:
                yield sse_event("report", item.report)
            else:
                citations = GenerationCitations(
                    appointment_id=item.appointment_id, service_id=item.service_id
                )
                yield sse_event("citations", citations)
            yield sse_event(
                "done",
                GenerationDone(
                    kind=item.kind,
                    outcome=item.outcome,
                    generated_content_id=item.generated_content_id,
                    interaction_id=item.interaction_id,
                ),
            )

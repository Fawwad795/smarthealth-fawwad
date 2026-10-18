"""The patient assistant: POST /assistant/ask (tasks 5.3 and 5.4).

ask() answers one question in this order:

1. Retrieval finds the offered services closest to the question, above the
   threshold (app/ai/retrieval.py), and loads what the answer prompt shows
   of each. Every path needs them: an answer is grounded in them, and a
   refusal names them so the patient knows where to go.
2. check_safety() labels the question. medical_advice, or no label, gets
   the fixed refusal.
3. Nothing found: the fixed "we don't offer that".
4. Otherwise the model answers from PROMPT_NAV_V1, shown only those services.

Retrieval runs before the safety check, not after as the brief lists them,
so all the database work is done before the first model call. The safety
check still runs before any answer is written. PREPARATION, AVAILABILITY
and OTHER questions are answered from the services too, until task 5.5
adds the patient's own appointments.

Refusals and "we don't offer that" are fixed text from app/ai/prompts.py,
never the model's words. Every reply ends with the disclaimer, and every
question, failed ones included, writes one ai_interactions row.

Embedding the question is CPU work, and every database call waits on the
network, so both run in a worker thread through asyncio.to_thread. The
model calls are awaited directly: they wait on the network without
blocking. The event loop stays free to serve other requests, bookings
included, the whole time.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.embeddings import EmbeddingProvider
from app.ai.llm import LLMError, LLMProvider, LLMUsage, get_llm_provider
from app.ai.prompts import (
    DISCLAIMER,
    FixedReply,
    ServiceContext,
    build_navigation_prompt,
    build_safety_prompt,
    no_match_reply,
    parse_intent,
    refusal_reply,
)
from app.ai.retrieval import RetrievedService, search_services
from app.core.config import settings
from app.core.exceptions import AppError
from app.models import AIInteraction, Service
from app.models.enums import AssistantIntent, InteractionOutcome
from app.services.ai_interaction import record_interaction

logger = logging.getLogger(__name__)

# The longest reply the safety check may produce. The answer is one label,
# a few tokens, so the cap only limits what a rambling model can cost. A
# reply cut short names no label and is refused: the safe side.
SAFETY_MAX_TOKENS = 16

# Shown to the model for a service with no description or preparation
# notes, so it says they are missing instead of filling the gap itself.
NONE_LISTED = "None listed."


@dataclass(frozen=True)
class SafetyCheck:
    """What the safety check made of one question, and what it cost."""

    intent: AssistantIntent | None
    prompt_version: str
    usage: LLMUsage

    @property
    def refused(self) -> bool:
        """Whether the question gets the refusal instead of an answer.

        MEDICAL_ADVICE is refused, and so is no label (None). A reply that
        is not a label means the model could not sort the question, so
        nothing shows the question is safe to answer.
        """
        return self.intent is None or self.intent is AssistantIntent.MEDICAL_ADVICE


@dataclass(frozen=True)
class AssistantAnswer:
    """What ask() returns: the reply, how the question ended, and the
    services the reply was built from."""

    text: str
    intent: AssistantIntent | None
    outcome: InteractionOutcome
    citations: list[RetrievedService]
    interaction_id: int


@dataclass
class _Progress:
    """What one question has gathered so far.

    ask() records it in ai_interactions however the question ends, so a
    failure halfway still keeps the ids and tokens gathered before it.
    """

    started: float
    services: list[RetrievedService] = field(default_factory=list)
    model: str | None = None
    intent: AssistantIntent | None = None
    safety_prompt_version: str | None = None
    prompt_version: str | None = None
    usages: list[LLMUsage] = field(default_factory=list)


def get_assistant_llm() -> LLMProvider:
    """The model for an assistant request, as a FastAPI dependency.

    A dependency rather than a direct call so tests can swap in FakeLLM
    through app.dependency_overrides. A provider that cannot be built (no
    key) is a 503: the assistant is unavailable, nothing is wrong with the
    request.
    """
    try:
        return get_llm_provider()
    except LLMError as err:
        raise _assistant_unavailable(err) from None


async def ask(
    db: Session,
    *,
    user_id: int,
    question: str,
    llm: LLMProvider,
    embeddings: EmbeddingProvider,
) -> AssistantAnswer:
    """Answer `question` for the user `user_id`, and record it.

    Raises a 503 AppError if the model or retrieval is unavailable, after
    recording the question as FAILED.
    """
    progress = _Progress(started=time.monotonic())
    try:
        text, outcome = await _reply(db, question, llm, embeddings, progress)
    except LLMError as err:
        await _record(db, user_id, progress, InteractionOutcome.FAILED)
        raise _assistant_unavailable(err) from None
    except AppError:
        # Retrieval could not embed the question, and has said so as a 503.
        await _record(db, user_id, progress, InteractionOutcome.FAILED)
        raise
    interaction = await _record(db, user_id, progress, outcome, answer_chars=len(text))
    return AssistantAnswer(
        text=text,
        intent=progress.intent,
        outcome=outcome,
        citations=progress.services,
        interaction_id=interaction.id,
    )


async def check_safety(question: str, llm: LLMProvider) -> SafetyCheck:
    """Ask the model which AssistantIntent `question` is.

    A failed call raises LLMError rather than returning a label: with no
    safety check there is no answer, and the caller tells the patient the
    assistant is unavailable.
    """
    prompt = build_safety_prompt(question, clinic=settings.assistant_clinic_name)
    result = await llm.complete(prompt.messages, max_tokens=SAFETY_MAX_TOKENS)
    return SafetyCheck(
        intent=parse_intent(result.text),
        prompt_version=prompt.version,
        usage=result.usage,
    )


def refusal(services: list[RetrievedService]) -> FixedReply:
    """The reply to a refused question, ending with the disclaimer.

    `services` are the offered services retrieval found for the question.
    The refusal names them, so the patient knows where a clinician can
    help; with none, it suggests a consultation.
    """
    reply = refusal_reply(services)
    return FixedReply(reply.version, with_disclaimer(reply.text))


def not_offered() -> FixedReply:
    """The reply when retrieval found nothing above the threshold, ending
    with the disclaimer."""
    reply = no_match_reply(clinic=settings.assistant_clinic_name)
    return FixedReply(reply.version, with_disclaimer(reply.text))


def with_disclaimer(text: str) -> str:
    """`text`, then the brief's disclaimer on its own line."""
    return f"{text}\n\n{DISCLAIMER}"


async def _reply(
    db: Session,
    question: str,
    llm: LLMProvider,
    embeddings: EmbeddingProvider,
    progress: _Progress,
) -> tuple[str, InteractionOutcome]:
    """The reply to `question` and how the question ended, filling in
    `progress` as each step finishes."""
    found = await asyncio.to_thread(_find_services, db, question, embeddings)
    progress.services = [service for service, _ in found]

    progress.model = llm.model
    check = await check_safety(question, llm)
    progress.intent = check.intent
    progress.safety_prompt_version = check.prompt_version
    progress.usages.append(check.usage)

    if check.refused:
        fixed = refusal(progress.services)
        progress.prompt_version = fixed.version
        return fixed.text, InteractionOutcome.REFUSED
    if not found:
        fixed = not_offered()
        progress.prompt_version = fixed.version
        return fixed.text, InteractionOutcome.NOT_OFFERED

    prompt = build_navigation_prompt(
        question,
        [context for _, context in found],
        clinic=settings.assistant_clinic_name,
    )
    progress.prompt_version = prompt.version
    result = await llm.complete(prompt.messages)
    progress.usages.append(result.usage)
    return with_disclaimer(result.text.strip()), InteractionOutcome.ANSWERED


def _find_services(
    db: Session, question: str, embeddings: EmbeddingProvider
) -> list[tuple[RetrievedService, ServiceContext]]:
    """The offered services retrieval finds for `question`, most similar
    first, each with what the answer prompt shows of it.

    Runs in a worker thread: embedding is CPU work, and each query waits
    on the database.

    Ends the session's transaction before returning. An open transaction
    keeps a connection checked out of the pool, and held through the
    seconds the model takes, enough waiting questions would leave none for
    a booking.
    """
    services = search_services(db, question, settings.retrieval_top_k, embeddings)
    # Columns, not Service objects: plain values the prompt can use as
    # they are. One query for every service found.
    notes = {
        service_id: (description, preparation)
        for service_id, description, preparation in db.execute(
            select(Service.id, Service.description, Service.prep_instructions).where(
                Service.id.in_([s.service_id for s in services])
            )
        )
    }
    # Nothing here wrote, so commit() and rollback() both end the read
    # transaction; rollback() would also undo anything the caller had
    # flushed.
    db.commit()
    return [
        (
            service,
            ServiceContext(
                name=service.name,
                department=service.department,
                description=notes[service.service_id][0] or NONE_LISTED,
                preparation=notes[service.service_id][1] or NONE_LISTED,
            ),
        )
        for service in services
    ]


async def _record(
    db: Session,
    user_id: int,
    progress: _Progress,
    outcome: InteractionOutcome,
    *,
    answer_chars: int = 0,
) -> AIInteraction:
    """Write the ai_interactions row for one question, in a worker thread:
    the commit waits on the database."""
    return await asyncio.to_thread(
        record_interaction,
        db,
        user_id=user_id,
        outcome=outcome,
        latency_ms=round((time.monotonic() - progress.started) * 1000),
        intent=progress.intent,
        service_ids=[service.service_id for service in progress.services],
        model=progress.model,
        safety_prompt_version=progress.safety_prompt_version,
        prompt_version=progress.prompt_version,
        usages=progress.usages,
        answer_chars=answer_chars,
    )


def _assistant_unavailable(err: LLMError) -> AppError:
    """The 503 for a model that could not be built or reached.

    The error's own message is logged, not returned: the patient only needs
    to know the assistant is down. LLMError messages carry status codes,
    never the messages sent (app/ai/llm.py), so logging one is safe.
    """
    logger.warning("assistant unavailable: %s", err)
    return AppError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="ASSISTANT_UNAVAILABLE",
        message="The assistant is unavailable right now. Please try again shortly.",
    )

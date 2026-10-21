"""The patient assistant: POST /assistant/ask and /assistant/ask/stream
(tasks 5.3, 5.4, 5.5 and 5.8).

ask() returns the whole reply at once; ask_stream() streams it. Both
answer one question in this order:

1. Retrieval finds the offered services closest to the question, above the
   threshold (app/ai/retrieval.py), and loads what the answer prompts show
   of each. Every path needs them: an answer is grounded in them, and a
   refusal names them so the patient knows where to go.
   The answer cache is checked next, since the services are part of its
   key (app/services/answer_cache.py). A hit is the reply, with no model
   call.
2. check_safety() labels the question. medical_advice, or no label, gets
   the fixed refusal.
3. preparation or availability: the model answers from PROMPT_PREP_V2,
   shown the caller's own upcoming appointments and the services found,
   each with its next open times.
4. Anything else with nothing found: the fixed "we don't offer that".
5. Otherwise the model answers from PROMPT_NAV_V1, shown only the services
   found.

Retrieval runs before the safety check, not after as the brief lists them,
so its database work is done before the first model call. The safety check
still runs before any answer is written.

The caller's appointments are read only for a preparation or availability
question, and only the caller's: the query is scoped by the user id from
their token, never by anything in the question. Such a question goes to the
model even when nothing was found, because "What should I bring tomorrow?"
names no service and its answer is in the patient's own appointments.

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
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import anyio
from fastapi import status
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.embeddings import EmbeddingProvider
from app.ai.llm import LLMError, LLMProvider, LLMUsage, get_llm_provider
from app.ai.prompts import (
    DISCLAIMER,
    AppointmentContext,
    BuiltPrompt,
    FixedReply,
    ServiceContext,
    build_navigation_prompt,
    build_preparation_prompt,
    build_safety_prompt,
    no_match_reply,
    parse_intent,
    refusal_reply,
)
from app.ai.retrieval import RetrievedService, search_services
from app.core.config import settings
from app.core.exceptions import AppError
from app.models import (
    AIInteraction,
    Appointment,
    Clinic,
    Department,
    Patient,
    ProviderService,
    Service,
    Slot,
)
from app.models.enums import (
    AppointmentStatus,
    AssistantIntent,
    InteractionOutcome,
    SlotStatus,
)
from app.services import answer_cache
from app.services.ai_interaction import record_interaction
from app.services.answer_cache import CachedAnswer, CacheUnavailable

logger = logging.getLogger(__name__)

# The longest reply the safety check may produce. The answer is one label,
# a few tokens, so the cap only limits what a rambling model can cost. A
# reply cut short names no label and is refused: the safe side.
SAFETY_MAX_TOKENS = 16

# Shown to the model for a service with no description or preparation
# notes, so it says they are missing instead of filling the gap itself.
NONE_LISTED = "None listed."

# The questions answered from the patient's own appointments and the
# services' open times, with PROMPT_PREP_V2.
BOOKING_INTENTS = (AssistantIntent.PREPARATION, AssistantIntent.AVAILABILITY)

# The ways a question can end whose reply is complete and can be cached.
# A FAILED or INTERRUPTED reply is not whole, so it is never stored.
CACHEABLE_OUTCOMES = (
    InteractionOutcome.ANSWERED,
    InteractionOutcome.REFUSED,
    InteractionOutcome.NOT_OFFERED,
)

# An appointment still going ahead: the saga has not ended it.
UPCOMING_STATUSES = (
    AppointmentStatus.REQUESTED,
    AppointmentStatus.SLOT_RESERVED,
    AppointmentStatus.CONFIRMED,
)

# How many of the patient's appointments, and of each service's free
# slots, the model is shown. Enough to answer "when" without sending a
# whole calendar to the model provider.
APPOINTMENTS_SHOWN = 5
OPEN_TIMES_SHOWN = 3

# What the patient is told when the model fails: in the 503 before a reply
# starts, and in the stream's error event after.
UNAVAILABLE_CODE = "ASSISTANT_UNAVAILABLE"
UNAVAILABLE_MESSAGE = (
    "The assistant is unavailable right now. Please try again shortly."
)


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
class _FixedAnswer:
    """A reply that needs no model: the refusal or "we don't offer that",
    already ending with the disclaimer."""

    version: str
    text: str
    outcome: InteractionOutcome


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
    appointment_ids: list[int] = field(default_factory=list)
    slot_ids: list[int] = field(default_factory=list)
    model: str | None = None
    intent: AssistantIntent | None = None
    safety_prompt_version: str | None = None
    prompt_version: str | None = None
    usages: list[LLMUsage] = field(default_factory=list)
    # The answer cache key, or None if Redis could not be reached for the
    # lookup: then the reply is not stored either.
    cache_key: str | None = None
    cache_hit: bool = False


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
    redis_client: Redis,
) -> AssistantAnswer:
    """Answer `question` for the user `user_id`, and record it.

    Raises a 503 AppError if the model or retrieval is unavailable, after
    recording the question as FAILED.
    """
    progress = _Progress(started=time.monotonic())
    async with _failures_recorded(db, user_id, progress):
        prepared = await _prepare(
            db, user_id, question, llm, embeddings, redis_client, progress
        )
        if isinstance(prepared, _FixedAnswer):
            text, outcome = prepared.text, prepared.outcome
        else:
            result = await llm.complete(prepared.messages)
            progress.usages.append(result.usage)
            text = with_disclaimer(result.text.strip())
            outcome = InteractionOutcome.ANSWERED
    interaction = await _record(db, user_id, progress, outcome, answer_chars=len(text))
    await _remember(redis_client, progress, outcome, text)
    return AssistantAnswer(
        text=text,
        intent=progress.intent,
        outcome=outcome,
        citations=progress.services,
        interaction_id=interaction.id,
    )


async def ask_stream(
    db: Session,
    *,
    user_id: int,
    question: str,
    llm: LLMProvider,
    embeddings: EmbeddingProvider,
    redis_client: Redis,
) -> AsyncGenerator[str | AssistantAnswer, None]:
    """Do everything before the answer, then return the answer as a stream
    (task 5.8).

    Retrieval and the safety check run here, before the route sends
    anything, so a failure in them is still an ordinary 503, recorded as
    FAILED, exactly as in ask(). Only the answer itself is streamed.

    The stream yields the reply's text piece by piece, then one
    AssistantAnswer saying how it ended.
    """
    progress = _Progress(started=time.monotonic())
    async with _failures_recorded(db, user_id, progress):
        prepared = await _prepare(
            db, user_id, question, llm, embeddings, redis_client, progress
        )
    return _stream_answer(db, user_id, prepared, llm, redis_client, progress)


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


@asynccontextmanager
async def _failures_recorded(
    db: Session, user_id: int, progress: _Progress
) -> AsyncIterator[None]:
    """Record the question as FAILED if the block raises, then raise the
    503 the patient sees.

    A model failure becomes ASSISTANT_UNAVAILABLE. Retrieval's failure is
    already an AppError (SEARCH_UNAVAILABLE) and is raised as it is.
    """
    try:
        yield
    except LLMError as err:
        await _record(db, user_id, progress, InteractionOutcome.FAILED)
        raise _assistant_unavailable(err) from None
    except AppError:
        await _record(db, user_id, progress, InteractionOutcome.FAILED)
        raise


async def _prepare(
    db: Session,
    user_id: int,
    question: str,
    llm: LLMProvider,
    embeddings: EmbeddingProvider,
    redis_client: Redis,
    progress: _Progress,
) -> BuiltPrompt | _FixedAnswer:
    """Everything before the answer is written: retrieval, the answer cache,
    the safety check and, for a booking question, the caller's appointments.

    Returns the prompt for the model to answer, or a reply that needs no
    model: a fixed one, or a cached one. ask() and ask_stream() differ only
    in how they get the model's answer. Fills in `progress` as each step
    finishes.
    """
    found = await asyncio.to_thread(_find_services, db, question, embeddings)
    progress.services = [service for service, _ in found]

    cached = await _cached(redis_client, question, found, llm, progress)
    if cached is not None:
        return cached

    progress.model = llm.model
    check = await check_safety(question, llm)
    progress.intent = check.intent
    progress.safety_prompt_version = check.prompt_version
    progress.usages.append(check.usage)

    if check.refused:
        fixed = refusal(progress.services)
        progress.prompt_version = fixed.version
        return _FixedAnswer(fixed.version, fixed.text, InteractionOutcome.REFUSED)

    if check.intent in BOOKING_INTENTS:
        appointments, open_slots = await asyncio.to_thread(
            _find_bookings, db, user_id, progress.services
        )
        progress.appointment_ids = [
            appointment_id for appointment_id, _ in appointments
        ]
        # A slot is the provider's time, so it comes back once for each
        # service found that the provider offers. Recorded once.
        progress.slot_ids = list(dict.fromkeys(slot_id for slot_id, _, _ in open_slots))
        services = [
            replace(context, open_times=_times_of(service.service_id, open_slots))
            for service, context in found
        ]
        prompt = build_preparation_prompt(
            question,
            [context for _, context in appointments],
            services,
            clinic=settings.assistant_clinic_name,
        )
        progress.prompt_version = prompt.version
        return prompt

    if not found:
        fixed = not_offered()
        progress.prompt_version = fixed.version
        return _FixedAnswer(fixed.version, fixed.text, InteractionOutcome.NOT_OFFERED)

    prompt = build_navigation_prompt(
        question,
        [context for _, context in found],
        clinic=settings.assistant_clinic_name,
    )
    progress.prompt_version = prompt.version
    return prompt


async def _cached(
    redis_client: Redis,
    question: str,
    found: list[tuple[RetrievedService, ServiceContext]],
    llm: LLMProvider,
    progress: _Progress,
) -> _FixedAnswer | None:
    """The cached reply to `question`, or None to answer it as usual.

    A hit fills in `progress` from the entry: the intent, versions and
    model that wrote the reply, so its row says what produced it. Redis in
    a worker thread: a call to an unreachable Redis waits seconds.
    """
    key = answer_cache.answer_key(question, found, llm.model)
    try:
        entry = await asyncio.to_thread(answer_cache.lookup, redis_client, key)
    except CacheUnavailable:
        return None
    progress.cache_key = key
    if entry is None:
        return None
    progress.cache_hit = True
    progress.model = entry.model
    progress.intent = entry.intent
    progress.safety_prompt_version = entry.safety_prompt_version
    progress.prompt_version = entry.prompt_version
    return _FixedAnswer(entry.prompt_version, entry.text, entry.outcome)


async def _remember(
    redis_client: Redis,
    progress: _Progress,
    outcome: InteractionOutcome,
    text: str,
) -> None:
    """Cache a finished reply, if it is whole and holds nothing about the
    patient.

    Not after a hit (storing again would restart its TTL), and not after a
    failed lookup, since Redis is unreachable.
    """
    if progress.cache_key is None or progress.cache_hit:
        return
    if outcome not in CACHEABLE_OUTCOMES or progress.intent in BOOKING_INTENTS:
        return
    entry = CachedAnswer(
        text=text,
        outcome=outcome,
        intent=progress.intent,
        model=progress.model,
        safety_prompt_version=progress.safety_prompt_version,
        prompt_version=progress.prompt_version,
    )
    await asyncio.to_thread(answer_cache.store, redis_client, progress.cache_key, entry)


async def _stream_answer(
    db: Session,
    user_id: int,
    prepared: BuiltPrompt | _FixedAnswer,
    llm: LLMProvider,
    redis_client: Redis,
    progress: _Progress,
) -> AsyncGenerator[str | AssistantAnswer, None]:
    """Yield the reply as it is written, then record it and yield the
    AssistantAnswer.

    A fixed or cached reply is sent as one piece: nothing is being
    generated, so there is nothing to stream. A model's answer is passed on piece by piece as the
    provider sends it, then the disclaimer as a last piece.

    How it ends decides the outcome recorded:

    - the reply is sent in full: ANSWERED, or the fixed reply's outcome;
    - the provider fails part-way: FAILED. The patient already has a 200
      and part of the answer, so the failure can only be reported in the
      last item, which the route sends as an error event;
    - the patient disconnects: INTERRUPTED. Nothing more is yielded, and
      the row records how many characters were sent before they left.

    `db` was closed by get_db when the route returned, before streaming
    began: FastAPI 0.115 closes yield dependencies at that point. A closed
    SQLAlchemy Session can be used again; the write below checks out a
    fresh connection, and its commit hands it back.
    """
    # Each piece is added just before it is yielded. A yield hands the piece
    # over, and a disconnect then arrives at that same yield, so adding it
    # afterwards would miss a piece the patient was given.
    parts: list[str] = []
    # Until the reply has been sent in full, the patient leaving is how
    # it ends.
    outcome = InteractionOutcome.INTERRUPTED
    try:
        if isinstance(prepared, _FixedAnswer):
            # Set first: a refusal is a refusal whether or not the patient
            # stays to read it, and the refusal counter must count it.
            outcome = prepared.outcome
            parts.append(prepared.text)
            yield prepared.text
        else:
            # aclosing() closes the provider's stream, and its connection,
            # as soon as this generator stops, so a patient who leaves
            # stops the model writing too.
            async with aclosing(llm.stream(prepared.messages)) as chunks:
                async for chunk in chunks:
                    if chunk.usage is not None:
                        progress.usages.append(chunk.usage)
                    if chunk.text:
                        parts.append(chunk.text)
                        yield chunk.text
            ending = f"\n\n{DISCLAIMER}"
            parts.append(ending)
            yield ending
            outcome = InteractionOutcome.ANSWERED
    except LLMError as err:
        logger.warning("assistant stream failed: %s", err)
        outcome = InteractionOutcome.FAILED
    finally:
        # Runs however the reply ended. A disconnect reaches this generator
        # one of two ways: as GeneratorExit at a yield, when SSEResponse
        # closes it, or as a cancellation while it waits on the provider.
        # A cancelled request would also cancel this write at its first
        # await; the shield lets this one write finish.
        with anyio.CancelScope(shield=True):
            interaction = await _record(
                db,
                user_id,
                progress,
                outcome,
                answer_chars=sum(len(part) for part in parts),
            )
            await _remember(redis_client, progress, outcome, "".join(parts))
    yield AssistantAnswer(
        text="".join(parts),
        intent=progress.intent,
        outcome=outcome,
        citations=progress.services,
        interaction_id=interaction.id,
    )


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


def _find_bookings(
    db: Session, user_id: int, services: list[RetrievedService]
) -> tuple[list[tuple[int, AppointmentContext]], list[tuple[int, int, datetime]]]:
    """The caller's upcoming appointments, and the next free slots of
    `services`, for a preparation or availability question.

    Runs in a worker thread and ends the transaction before returning, for
    the same reason as _find_services: the answer's model call comes next.
    """
    now = datetime.now(UTC)
    appointments = _upcoming_appointments(db, user_id, now)
    open_slots = _open_slots(db, [service.service_id for service in services], now)
    db.commit()
    return appointments, open_slots


def _upcoming_appointments(
    db: Session, user_id: int, now: datetime
) -> list[tuple[int, AppointmentContext]]:
    """The caller's next appointments still going ahead, earliest first,
    each with its id.

    The PHI access control: the query is scoped to the patient record of
    `user_id`, the id from the caller's token, so nothing in the question
    can reach another patient's appointments. PROMPT_PREP_V2's "the
    patient's own appointments" only describes this; it enforces nothing.
    Only the fields AppointmentContext names are read: no name, contact
    details or notes ever reach the model provider.
    """
    rows = db.execute(
        select(
            Appointment.id,
            Service.name,
            Department.name,
            Slot.start_time,
            Appointment.status,
            Service.prep_instructions,
            Clinic.timezone,
        )
        .select_from(Appointment)
        .join(Patient, Patient.id == Appointment.patient_id)
        .join(Slot, Slot.id == Appointment.slot_id)
        .join(Service, Service.id == Appointment.service_id)
        .join(Department, Department.id == Service.department_id)
        .join(Clinic, Clinic.id == Department.clinic_id)
        .where(
            Patient.user_id == user_id,
            Appointment.status.in_(UPCOMING_STATUSES),
            Slot.start_time > now,
        )
        .order_by(Slot.start_time)
        .limit(APPOINTMENTS_SHOWN)
    )
    return [
        (
            appointment_id,
            AppointmentContext(
                service_name=service,
                department=department,
                starts_at=starts_at.astimezone(ZoneInfo(timezone)),
                status=appointment_status,
                preparation=preparation or NONE_LISTED,
            ),
        )
        for (
            appointment_id,
            service,
            department,
            starts_at,
            appointment_status,
            preparation,
            timezone,
        ) in rows
    ]


def _open_slots(
    db: Session, service_ids: list[int], now: datetime
) -> list[tuple[int, int, datetime]]:
    """The next OPEN_TIMES_SHOWN free slots of each service, as (slot id,
    service id, start in the clinic's timezone), earliest first.

    A service's slots are those of the providers who offer it
    (provider_services). row_number() numbers each service's free slots by
    start time, so one query keeps the first few of every service. Slots
    are not patient data, so nothing here is scoped to the caller.
    """
    if not service_ids:
        return []
    ranked = (
        select(
            Slot.id.label("slot_id"),
            ProviderService.service_id,
            Slot.start_time,
            Clinic.timezone,
            func.row_number()
            .over(
                partition_by=ProviderService.service_id,
                order_by=(Slot.start_time, Slot.id),
            )
            .label("position"),
        )
        .select_from(Slot)
        .join(ProviderService, ProviderService.provider_id == Slot.provider_id)
        .join(Service, Service.id == ProviderService.service_id)
        .join(Department, Department.id == Service.department_id)
        .join(Clinic, Clinic.id == Department.clinic_id)
        .where(
            ProviderService.service_id.in_(service_ids),
            Slot.status == SlotStatus.AVAILABLE,
            Slot.start_time > now,
        )
        .subquery()
    )
    rows = db.execute(
        select(
            ranked.c.slot_id,
            ranked.c.service_id,
            ranked.c.start_time,
            ranked.c.timezone,
        )
        .where(ranked.c.position <= OPEN_TIMES_SHOWN)
        .order_by(ranked.c.service_id, ranked.c.start_time, ranked.c.slot_id)
    )
    return [
        (slot_id, service_id, starts_at.astimezone(ZoneInfo(timezone)))
        for slot_id, service_id, starts_at, timezone in rows
    ]


def _times_of(
    service_id: int, open_slots: list[tuple[int, int, datetime]]
) -> tuple[datetime, ...]:
    """The open times of one service. Two providers free at the same time
    are one time to the patient, so each time is listed once."""
    times = [
        starts_at
        for _, slot_service, starts_at in open_slots
        if slot_service == service_id
    ]
    return tuple(dict.fromkeys(times))


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
        appointment_ids=progress.appointment_ids,
        slot_ids=progress.slot_ids,
        model=progress.model,
        safety_prompt_version=progress.safety_prompt_version,
        prompt_version=progress.prompt_version,
        usages=progress.usages,
        answer_chars=answer_chars,
        cache_hit=progress.cache_hit,
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
        code=UNAVAILABLE_CODE,
        message=UNAVAILABLE_MESSAGE,
    )

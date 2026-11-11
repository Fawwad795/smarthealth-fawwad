"""Staff generation: appointment summaries, follow-up drafts and utilisation
reports (tasks 5.9 and 5.10).

Each run has two stages, as in /assistant/ask/stream:

1. Before the stream, the facts are read from the database. A missing
   appointment is an ordinary 404 here, before anything is sent.
2. The stream passes the model's text on piece by piece. A report's reply
   is then validated as ReportCommentary. If it cannot be used, the model
   is asked once more with REPORT_REPAIR_V1, and a second failure ends the
   run cleanly as REPORT_INVALID.

Every number comes from the code. A summary or follow-up is built from one
appointment's service, department, time and status. A report's figures are
summarise_range(), the function behind GET /analytics/summary, plus each
department's booked slots. The model writes only words around them.

Every run, interrupted and failed ones included, saves one ai_interactions
row (intent STAFF_GENERATION) and one generated_content row holding what was
generated, with the prompt version and model.
"""

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from zoneinfo import ZoneInfo

import anyio
from fastapi import status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.llm import LLMError, LLMProvider, LLMUsage, get_llm_provider
from app.ai.prompts import (
    AppointmentFacts,
    BuiltPrompt,
    build_followup_prompt,
    build_repair_prompt,
    build_report_prompt,
    build_summary_prompt,
)
from app.core.config import settings
from app.core.exceptions import AppError
from app.models import Department, GeneratedContent, Provider, Slot
from app.models.enums import (
    AppointmentStatus,
    AssistantIntent,
    GenerationKind,
    InteractionOutcome,
    SlotStatus,
)
from app.schemas.analytics import AnalyticsSummaryResponse
from app.schemas.generation import (
    DepartmentUtilisation,
    ReportCommentary,
    UtilisationFigures,
    UtilisationReport,
)
from app.services.ai_interaction import record_interaction
from app.services.analytics import summarise_range
from app.services.appointment_scheduling import get_appointment
from app.services.assistant import NONE_LISTED

logger = logging.getLogger(__name__)

# The first reply, then the brief's one retry with a repair instruction.
REPORT_ATTEMPTS = 2

# An appointment's status as a patient-facing message may say it. The enum
# name ("SLOT_RESERVED") would otherwise end up in the message.
STATUS_WORDS = {
    AppointmentStatus.REQUESTED: "requested, not yet confirmed",
    AppointmentStatus.SLOT_RESERVED: "being confirmed",
    AppointmentStatus.CONFIRMED: "confirmed",
    AppointmentStatus.COMPLETED: "completed",
    AppointmentStatus.REJECTED: "not accepted",
    AppointmentStatus.CANCELLED: "cancelled",
}

UNAVAILABLE_CODE = "GENERATION_UNAVAILABLE"
UNAVAILABLE_MESSAGE = "The model is unavailable right now. Please try again shortly."
REPORT_INVALID_CODE = "REPORT_INVALID"
REPORT_INVALID_MESSAGE = (
    "The model's report failed validation twice, so no report was made. "
    "Please try again."
)

# A JSON reply wrapped in a Markdown code fence, which models add even when
# told not to.
_CODE_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)


@dataclass(frozen=True)
class ReportRetry:
    """Yielded when a report reply could not be used and the model is being
    asked again. `problem` says what was wrong; it is written by the code,
    never copied from the reply."""

    problem: str


@dataclass(frozen=True)
class GenerationResult:
    """The last item a generation stream yields: how the run ended and
    what it saved.

    `report` is set for a validated report, `error_code` and
    `error_message` for a failed run.
    """

    kind: GenerationKind
    outcome: InteractionOutcome
    interaction_id: int
    generated_content_id: int
    appointment_id: int | None = None
    service_id: int | None = None
    report: UtilisationReport | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class _Job:
    """What one run generates, and from what."""

    kind: GenerationKind
    prompt: BuiltPrompt
    appointment_id: int | None = None
    service_id: int | None = None
    # A report's figures. Set for a report only, so it also says the reply
    # must be validated.
    figures: UtilisationFigures | None = None


@dataclass
class _Run:
    """What one run has done so far, saved however it ends."""

    started: float
    # The prompt the current reply answers: the job's own, or the repair
    # prompt once a report has been retried.
    prompt: BuiltPrompt
    # Every piece streamed, across both report attempts: what the staff
    # member saw.
    parts: list[str] = field(default_factory=list)
    usages: list[LLMUsage] = field(default_factory=list)


def get_generation_llm() -> LLMProvider:
    """The model for a generation request, as a FastAPI dependency.

    A dependency so tests can swap in FakeLLM. A provider that cannot be
    built (no key) is a 503, before any stream starts.
    """
    try:
        return get_llm_provider()
    except LLMError as err:
        logger.warning("generation unavailable: %s", err)
        raise AppError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=UNAVAILABLE_CODE,
            message=UNAVAILABLE_MESSAGE,
        ) from None


async def generate_for_appointment(
    db: Session,
    *,
    user_id: int,
    appointment_id: int,
    kind: GenerationKind,
    llm: LLMProvider,
) -> AsyncGenerator[str | ReportRetry | GenerationResult, None]:
    """Read one appointment, then return the stream of its summary or
    follow-up draft.

    Raises the 404 AppError for an unknown appointment before any stream
    exists. The stream yields the text piece by piece, then one
    GenerationResult.
    """
    started = time.monotonic()
    facts, service_id = await asyncio.to_thread(_appointment_facts, db, appointment_id)
    build = (
        build_summary_prompt
        if kind is GenerationKind.SUMMARY
        else build_followup_prompt
    )
    job = _Job(
        kind=kind,
        prompt=build(facts, clinic=settings.assistant_clinic_name),
        appointment_id=appointment_id,
        service_id=service_id,
    )
    return _stream(db, user_id, job, llm, started)


async def generate_report(
    db: Session,
    *,
    user_id: int,
    start_date: date,
    end_date: date,
    llm: LLMProvider,
) -> AsyncGenerator[str | ReportRetry | GenerationResult, None]:
    """Read the figures for a date range, then return the stream of the
    report's commentary.

    The stream yields the model's JSON piece by piece, a ReportRetry if the
    first reply could not be used, then one GenerationResult carrying the
    validated report.
    """
    started = time.monotonic()
    figures = await asyncio.to_thread(report_figures, db, start_date, end_date)
    job = _Job(
        kind=GenerationKind.UTILISATION_REPORT,
        prompt=build_report_prompt(
            figures.model_dump_json(indent=2), clinic=settings.assistant_clinic_name
        ),
        figures=figures,
    )
    return _stream(db, user_id, job, llm, started)


def report_figures(db: Session, start_date: date, end_date: date) -> UtilisationFigures:
    """Every number in a utilisation report for the range, both days
    included.

    `analytics` is summarise_range(), validated by the same schema as GET
    /analytics/summary, so the two always agree. Runs in a worker thread
    and ends its transaction before the model is called, as the assistant
    does.
    """
    analytics = AnalyticsSummaryResponse.model_validate(
        summarise_range(db, start_date, end_date)
    )
    departments = _department_utilisation(db, start_date, end_date)
    db.commit()
    return UtilisationFigures(analytics=analytics, departments=departments)


def parse_report(reply: str, figures: UtilisationFigures) -> UtilisationReport:
    """Validate the model's report reply and join it to the figures.

    Raises pydantic's ValidationError, for malformed JSON as well as for a
    missing field, an extra key or a digit. Lenient only about a code fence
    around the JSON.
    """
    text = reply.strip()
    fenced = _CODE_FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    commentary = ReportCommentary.model_validate_json(text)
    return UtilisationReport(
        figures=figures,
        summary=commentary.summary,
        observations=commentary.observations,
    )


def describe_problem(err: ValidationError) -> str:
    """What was wrong with a report reply, for the repair prompt and the
    retry event.

    Each error's location and message only. Pydantic also records the
    input, which is the model's own text, and that is left out.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'reply'}: {error['msg']}"
        for error in err.errors(include_url=False)
    )


async def _stream(
    db: Session,
    user_id: int,
    job: _Job,
    llm: LLMProvider,
    started: float,
) -> AsyncGenerator[str | ReportRetry | GenerationResult, None]:
    """Yield the model's text as it is written, validate a report, then
    save the run and yield its GenerationResult.

    A report gets REPORT_ATTEMPTS tries: after an unusable reply the model
    is shown that reply and what was wrong with it. Ends as ANSWERED,
    FAILED (the provider failed, or both report replies were unusable), or
    INTERRUPTED when the staff member disconnects, which yields nothing
    more. The disconnect handling is /assistant/ask/stream's: SSEResponse
    closes this generator, and the shield lets the final write finish.
    """
    run = _Run(started=started, prompt=job.prompt)
    report: UtilisationReport | None = None
    error: tuple[str, str] | None = None
    outcome = InteractionOutcome.INTERRUPTED
    try:
        attempts = REPORT_ATTEMPTS if job.figures is not None else 1
        for attempt in range(1, attempts + 1):
            reply: list[str] = []
            async with aclosing(llm.stream(run.prompt.messages)) as chunks:
                async for chunk in chunks:
                    if chunk.usage is not None:
                        run.usages.append(chunk.usage)
                    if chunk.text:
                        # Added before the yield, as in the assistant, so a
                        # piece handed over is never missed.
                        run.parts.append(chunk.text)
                        reply.append(chunk.text)
                        yield chunk.text
            if job.figures is None:
                outcome = InteractionOutcome.ANSWERED
                break
            try:
                report = parse_report("".join(reply), job.figures)
            except ValidationError as err:
                problem = describe_problem(err)
                logger.info("report reply %d unusable: %s", attempt, problem)
                if attempt == attempts:
                    outcome = InteractionOutcome.FAILED
                    error = (REPORT_INVALID_CODE, REPORT_INVALID_MESSAGE)
                    break
                yield ReportRetry(problem)
                run.prompt = build_repair_prompt(run.prompt, "".join(reply), problem)
                continue
            outcome = InteractionOutcome.ANSWERED
            break
    except LLMError as err:
        logger.warning("generation failed: %s", err)
        outcome = InteractionOutcome.FAILED
        error = (UNAVAILABLE_CODE, UNAVAILABLE_MESSAGE)
    finally:
        content = (
            report.model_dump(mode="json") if report else {"text": "".join(run.parts)}
        )
        # Runs however the run ended. See _stream_answer in
        # app/services/assistant.py for why the write is shielded.
        with anyio.CancelScope(shield=True):
            interaction_id, generated_content_id = await asyncio.to_thread(
                _save, db, user_id, job, run, llm.model, outcome, content
            )
    yield GenerationResult(
        kind=job.kind,
        outcome=outcome,
        interaction_id=interaction_id,
        generated_content_id=generated_content_id,
        appointment_id=job.appointment_id,
        service_id=job.service_id,
        report=report,
        error_code=error[0] if error else None,
        error_message=error[1] if error else None,
    )


def _appointment_facts(
    db: Session, appointment_id: int
) -> tuple[AppointmentFacts, int]:
    """The facts a summary or follow-up is written from, and the
    appointment's service id. Raises the 404 for an unknown appointment.

    Only the fields AppointmentFacts names leave this function: never the
    patient's name, contact details or date of birth. Runs in a worker
    thread and ends its transaction before the model is called.
    """
    appointment = get_appointment(db, appointment_id)
    service = appointment.service
    facts = AppointmentFacts(
        service_name=service.name,
        department=service.department.name,
        specialty=appointment.provider.specialty.name,
        starts_at=appointment.slot.start_time.astimezone(
            ZoneInfo(service.department.clinic.timezone)
        ),
        status=STATUS_WORDS[appointment.status],
        preparation=service.prep_instructions or NONE_LISTED,
    )
    service_id = service.id
    db.commit()
    return facts, service_id


def _department_utilisation(
    db: Session, start_date: date, end_date: date
) -> list[DepartmentUtilisation]:
    """Each department's slots starting in the range, and how many are
    BOOKED, in the departments' display order.

    A slot belongs to its provider's department. BLOCKED slots are time
    off, never bookable, so they are left out of the total. The range is
    UTC calendar days, as analytics_daily counts them. A department with
    no slots still appears, with no utilisation.
    """
    range_start = datetime.combine(start_date, clock_time.min, tzinfo=UTC)
    range_end = datetime.combine(end_date, clock_time.min, tzinfo=UTC) + timedelta(
        days=1
    )
    rows = db.execute(
        select(
            Department.id,
            Department.name,
            func.count(Slot.id),
            func.count(Slot.id).filter(Slot.status == SlotStatus.BOOKED),
        )
        .select_from(Department)
        .outerjoin(Provider, Provider.department_id == Department.id)
        .outerjoin(
            Slot,
            (Slot.provider_id == Provider.id)
            & (Slot.start_time >= range_start)
            & (Slot.start_time < range_end)
            & (Slot.status != SlotStatus.BLOCKED),
        )
        .group_by(Department.id)
        .order_by(Department.order_index, Department.id)
    )
    return [
        DepartmentUtilisation(
            department_id=department_id,
            department=name,
            slots=slots,
            booked_slots=booked,
            utilisation=round(booked / slots, 3) if slots else None,
        )
        for department_id, name, slots, booked in rows
    ]


def _save(
    db: Session,
    user_id: int,
    job: _Job,
    run: _Run,
    model: str,
    outcome: InteractionOutcome,
    content: dict[str, Any],
) -> tuple[int, int]:
    """Write the run's ai_interactions and generated_content rows, and
    return their ids. Runs in a worker thread.

    Two commits: record_interaction() commits its own row, and the content
    row needs that row's id. A failure between the two would leave the
    interaction without its content; both are single inserts, one after
    the other.
    """
    interaction = record_interaction(
        db,
        user_id=user_id,
        outcome=outcome,
        latency_ms=round((time.monotonic() - run.started) * 1000),
        intent=AssistantIntent.STAFF_GENERATION,
        service_ids=[job.service_id] if job.service_id is not None else [],
        appointment_ids=[job.appointment_id] if job.appointment_id is not None else [],
        model=model,
        prompt_version=run.prompt.version,
        usages=run.usages,
        answer_chars=sum(len(part) for part in run.parts),
    )
    figures = job.figures
    generated = GeneratedContent(
        kind=job.kind,
        appointment_id=job.appointment_id,
        report_start=figures.analytics.start_date if figures else None,
        report_end=figures.analytics.end_date if figures else None,
        content=content,
        model=model,
        prompt_version=run.prompt.version,
        created_by=user_id,
        interaction_id=interaction.id,
    )
    db.add(generated)
    db.commit()
    logger.info(
        "generated content %d: kind=%s outcome=%s interaction=%d",
        generated.id,
        job.kind,
        outcome,
        interaction.id,
    )
    return interaction.id, generated.id

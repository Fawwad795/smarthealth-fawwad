"""AI analytics (task 5.11): what the assistant and staff generation did
over a date range, counted from ai_interactions.

Counted from the rows on each request, unlike the six operational metrics,
which are read from analytics_daily. The p95 latency needs every latency in
the range, which a stored sum and count cannot give. The range is capped at
366 days (DateRange), and ix_ai_interactions_created_at serves the filter.

Booking conversion: of the patients who asked the assistant something in the
range, how many booked an appointment within CONVERSION_WINDOW after one of
their questions.
"""

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import ColumnElement, and_, distinct, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models import AIInteraction, Appointment, Patient
from app.models.enums import AssistantIntent, InteractionOutcome

# A booking within a day of a question counts as following from it: long
# enough for a patient who asks in the evening and books the next morning,
# short enough that a booking a week later is not credited to the assistant.
CONVERSION_WINDOW = timedelta(hours=24)

# The key for interactions the safety check gave no intent, matching the
# Prometheus label.
NO_INTENT = "none"


def summarise_ai(db: Session, start_date: date, end_date: date) -> dict[str, object]:
    """The AI analytics for the UTC days start_date to end_date, inclusive.

    Averages and the conversion rate are None, not 0, when there is nothing
    to divide, as in summarise_range().
    """
    # Half-open at the top, so the last day is whole.
    range_start = datetime.combine(start_date, time.min, tzinfo=UTC)
    range_end = datetime.combine(end_date, time.min, tzinfo=UTC) + timedelta(days=1)
    in_range = and_(
        AIInteraction.created_at >= range_start,
        AIInteraction.created_at < range_end,
    )

    total, avg_latency, p95_latency, input_tokens, output_tokens = db.execute(
        select(
            func.count(),
            func.avg(AIInteraction.latency_ms),
            # Interpolates between the two nearest latencies, as numpy's
            # default percentile does.
            func.percentile_cont(0.95).within_group(AIInteraction.latency_ms),
            func.coalesce(func.sum(AIInteraction.input_tokens), 0),
            func.coalesce(func.sum(AIInteraction.output_tokens), 0),
        ).where(in_range)
    ).one()

    outcomes = _count_by(db, AIInteraction.outcome, in_range)
    intents = _count_by(db, AIInteraction.intent, in_range)
    # Every enum value is a key, at 0 if it never occurred, so the response
    # has the same shape whatever happened.
    by_outcome = {
        outcome.value: outcomes.get(outcome, 0) for outcome in InteractionOutcome
    }
    by_intent = {intent.value: intents.get(intent, 0) for intent in AssistantIntent}
    by_intent[NO_INTENT] = intents.get(None, 0)
    staff_generations = by_intent[AssistantIntent.STAFF_GENERATION]

    patients_asked, patients_booked = _booking_conversion(db, in_range)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "interactions": total,
        "questions_asked": total - staff_generations,
        "staff_generations": staff_generations,
        "by_outcome": by_outcome,
        "by_intent": by_intent,
        # avg() of an integer column comes back from Postgres as a Decimal.
        "avg_latency_ms": float(avg_latency) if avg_latency is not None else None,
        "p95_latency_ms": p95_latency,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "booking_conversion": {
            "window_hours": CONVERSION_WINDOW // timedelta(hours=1),
            "patients_asked": patients_asked,
            "patients_booked": patients_booked,
            "rate": patients_booked / patients_asked if patients_asked else None,
        },
    }


def _count_by(
    db: Session, column: InstrumentedAttribute, in_range: ColumnElement[bool]
) -> dict[object, int]:
    """Interactions in the range per value of `column`, as {value: count}.

    Only values that occurred are keys; a null intent is the key None.
    """
    rows = db.execute(select(column, func.count()).where(in_range).group_by(column))
    return dict(rows.tuples().all())


def _booking_conversion(db: Session, in_range: ColumnElement[bool]) -> tuple[int, int]:
    """How many patients asked a question in the range, and how many of them
    booked within CONVERSION_WINDOW after one of their questions.

    "Booked" means the appointment's booked_at, set when the scheduling saga
    confirms it, the column analytics_daily counts bookings by. An
    appointment cancelled since still counts: it was booked.
    """
    # Staff generations are not questions. A null intent is a question the
    # safety check could not sort, so it counts.
    is_question = or_(
        AIInteraction.intent.is_(None),
        AIInteraction.intent != AssistantIntent.STAFF_GENERATION,
    )
    # One of this user's appointments booked after the question and within
    # the window. Joined through patients, since an interaction stores the
    # user and an appointment the patient.
    booked_after = (
        select(Appointment.id)
        .join(Patient, Appointment.patient_id == Patient.id)
        .where(
            Patient.user_id == AIInteraction.user_id,
            Appointment.booked_at > AIInteraction.created_at,
            Appointment.booked_at <= AIInteraction.created_at + CONVERSION_WINDOW,
        )
        .exists()
    )
    asked, booked = db.execute(
        select(
            func.count(distinct(AIInteraction.user_id)),
            func.count(distinct(AIInteraction.user_id)).filter(booked_after),
        ).where(in_range, is_question)
    ).one()
    return asked, booked

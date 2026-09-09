"""Analytics: the aggregates the consumer maintains, and the raw truth to check them against.

Two functions with opposite jobs. increment_daily() is how analytics_daily
is *written* -- one event at a time, by the Kafka consumer, which is its
only writer. compute_analytics_for_date() never writes: it recomputes the
same numbers from the raw tables so task 3.7 can compare the two and
report drift.

Keeping the comparison read-only is the whole point. Until Week 3 Day 3 a
Celery Beat task recomputed and overwrote this table every five minutes,
which fought the consumer's increments and, worse, made a reconciliation
check meaningless -- it would have been comparing the aggregates against
the very query that had just written them.
"""

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    AnalyticsDaily,
    Appointment,
    AppointmentStatusHistory,
    Slot,
    Visit,
)
from app.models.enums import AppointmentStatus, VisitStatus


def compute_analytics_for_date(db: Session, target_date: date) -> dict[str, float]:
    """What the raw tables say one day's numbers should be. Writes nothing.

    Returns the same keys analytics_daily stores, so task 3.7 can compare
    the two dictionaries directly and report any that differ.

    Each bucket reads the same column the matching handler in
    app/events/handlers.py reads -- booked_at, the CANCELLED history
    row's created_at, completed_at, checked_in_at. That is deliberate: if
    the two disagreed about which column defines a day, this check would
    report drift that was an artefact of the check itself.
    """
    day_start = datetime.combine(target_date, time.min, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    appointments_booked = db.execute(
        select(func.count())
        .select_from(Appointment)
        .where(Appointment.booked_at >= day_start, Appointment.booked_at < day_end)
    ).scalar_one()

    completed_visits = db.execute(
        select(func.count())
        .select_from(Visit)
        .where(
            Visit.status == VisitStatus.COMPLETED,
            Visit.completed_at >= day_start,
            Visit.completed_at < day_end,
        )
    ).scalar_one()

    cancellations = db.execute(
        select(func.count())
        .select_from(AppointmentStatusHistory)
        .where(
            AppointmentStatusHistory.to_status == AppointmentStatus.CANCELLED,
            AppointmentStatusHistory.created_at >= day_start,
            AppointmentStatusHistory.created_at < day_end,
        )
    ).scalar_one()

    # The sum and the count, not the average: this has to return what
    # analytics_daily actually stores, and the row stores the two
    # components precisely because an average cannot be incremented.
    wait_seconds_total, wait_count = db.execute(
        select(
            func.coalesce(
                func.sum(func.extract("epoch", Visit.checked_in_at - Slot.start_time)),
                0,
            ),
            func.count(),
        )
        .select_from(Visit)
        .join(Appointment, Visit.appointment_id == Appointment.id)
        .join(Slot, Appointment.slot_id == Slot.id)
        .where(Visit.checked_in_at >= day_start, Visit.checked_in_at < day_end)
    ).one()

    return {
        "appointments_booked": appointments_booked,
        "completed_visits": completed_visits,
        "cancellations": cancellations,
        "wait_seconds_total": float(wait_seconds_total),
        "wait_count": wait_count,
    }


def increment_daily(db: Session, target_date: date, **deltas: float) -> None:
    """Add deltas to one day's row, creating the row if it is not there yet.

    INSERT ... ON CONFLICT DO UPDATE SET col = analytics_daily.col +
    excluded.col: one statement, so the arithmetic happens inside the
    database where every writer is visible. A read-then-write would lose
    an increment whenever two events for the same day overlapped -- the
    same check-then-act gap as the slot reservation, in a different
    costume.

    Deliberately does not commit. The caller is the consumer, holding the
    transaction that also carries the processed_events claim, and the two
    have to land together or not at all.
    """
    stmt = insert(AnalyticsDaily).values(date=target_date, **deltas)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AnalyticsDaily.date],
        set_={
            column: getattr(AnalyticsDaily, column) + getattr(stmt.excluded, column)
            for column in deltas
        }
        # onupdate is ORM-side and this is a Core statement, so the
        # timestamp has to be set explicitly or it never moves.
        | {"updated_at": func.now()},
    )
    db.execute(stmt)

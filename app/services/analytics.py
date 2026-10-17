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
    FailedJob,
    Patient,
    Slot,
    Visit,
)
from app.models.enums import AppointmentStatus, VisitStatus


def compute_analytics_for_date(db: Session, target_date: date) -> dict[str, float]:
    """What the raw tables say one day's numbers should be. Writes nothing.

    Returns the same keys analytics_daily stores, so task 3.7 can compare
    the two dictionaries directly and report any that differ.

    Each bucket reads the same column the matching handler in
    app/kafka/handlers.py reads -- booked_at, the CANCELLED history
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
        .where(
            # Completed only, and bucketed by check-in. Both halves matter.
            # handle_visit_completed is the only writer of these two
            # columns and it runs on visit.completed, so a visit that has
            # checked in but not been seen out has contributed nothing yet.
            # Counting it here would report drift against an aggregate that
            # is behaving correctly -- and since a clinic has someone
            # mid-visit for most of the working day, that phantom drift
            # would be the normal state of the report rather than the
            # exception.
            Visit.status == VisitStatus.COMPLETED,
            Visit.checked_in_at >= day_start,
            Visit.checked_in_at < day_end,
        )
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


def summarise_range(db: Session, start_date: date, end_date: date) -> dict[str, object]:
    """The six metrics for a date range.

    Four come from analytics_daily, summed -- the endpoint never touches
    appointments or visits. The other two are single scalar counts:
    nothing announces a new patient or a failed job, so no event could
    maintain an aggregate for them, and a stored copy would be a second
    source of truth for a number these tables already hold. Total
    patients is not a per-day figure at all.
    """
    booked, completed, cancellations, wait_total, wait_count = db.execute(
        select(
            func.coalesce(func.sum(AnalyticsDaily.appointments_booked), 0),
            func.coalesce(func.sum(AnalyticsDaily.completed_visits), 0),
            func.coalesce(func.sum(AnalyticsDaily.cancellations), 0),
            func.coalesce(func.sum(AnalyticsDaily.wait_seconds_total), 0.0),
            func.coalesce(func.sum(AnalyticsDaily.wait_count), 0),
        ).where(
            AnalyticsDaily.date >= start_date,
            AnalyticsDaily.date <= end_date,
        )
    ).one()

    total_patients = db.execute(select(func.count()).select_from(Patient)).scalar_one()

    # failed_jobs stores a timestamp, not a date, so the range has to be
    # widened into one -- half-open at the top so the last day is whole.
    range_start = datetime.combine(start_date, time.min, tzinfo=UTC)
    range_end = datetime.combine(end_date, time.min, tzinfo=UTC) + timedelta(days=1)
    failed_jobs = db.execute(
        select(func.count())
        .select_from(FailedJob)
        .where(FailedJob.created_at >= range_start, FailedJob.created_at < range_end)
    ).scalar_one()

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_patients": total_patients,
        "appointments_booked": booked,
        "completed_visits": completed,
        "cancellations": cancellations,
        # None, not 0: nothing was booked, so there is no rate to report.
        "cancellation_rate": (cancellations / booked) if booked else None,
        "avg_wait_seconds": (wait_total / wait_count) if wait_count else None,
        "failed_jobs": failed_jobs,
    }


def appointments_series(
    db: Session, start_date: date, end_date: date
) -> list[dict[str, object]]:
    """Appointments booked per day, one entry per day in the range.

    Days with no row are returned as zero rather than omitted.
    increment_daily only creates a row when something happens, so a quiet
    day is simply absent -- and a chart with holes in it is a worse
    answer than one with zeros, since the client would have to know the
    convention to draw it correctly.
    """
    booked_by_day = dict(
        db.execute(
            select(AnalyticsDaily.date, AnalyticsDaily.appointments_booked).where(
                AnalyticsDaily.date >= start_date,
                AnalyticsDaily.date <= end_date,
            )
        ).all()
    )

    day_count = (end_date - start_date).days + 1
    return [
        {
            "date": day,
            "appointments_booked": booked_by_day.get(day, 0),
        }
        for day in (start_date + timedelta(days=offset) for offset in range(day_count))
    ]


def reconcile_date(db: Session, target_date: date) -> dict[str, dict[str, float]]:
    """Compare one day's stored row against what the raw tables say.

    Returns only the fields that disagree, each as {"stored": x, "actual":
    y}. An empty dict means that day is in sync -- which is the answer we
    want almost always, so it is the cheapest one to return.

    A day with no stored row is treated as a row of zeros rather than
    skipped. That distinction matters: a quiet day with no row and no
    activity really is in sync, but a day where six appointments were
    booked and no row exists is drift of exactly the kind this exists to
    catch, and skipping missing rows would hide it.

    Writes nothing, and must never start. A check that repaired what it
    found could never report anything -- the evidence would be gone by the
    time it spoke -- so repair_date() is a separate, deliberate act.
    """
    actual = compute_analytics_for_date(db, target_date)
    row = db.get(AnalyticsDaily, target_date)
    stored: dict[str, float] = (
        {field: float(getattr(row, field)) for field in actual}
        if row is not None
        else dict.fromkeys(actual, 0.0)
    )

    return {
        field: {"stored": stored[field], "actual": float(actual[field])}
        for field in actual
        # A tolerance, not ==, because wait_seconds_total is a float built
        # by summing durations. Two arithmetically equal answers can differ
        # in the last bit or two, and reporting that as drift would train
        # everyone to ignore this check.
        if abs(stored[field] - float(actual[field])) > 1e-6
    }


def reconcile_range(
    db: Session, start_date: date, end_date: date
) -> list[dict[str, object]]:
    """Check every day in the range and return only the ones that disagree.

    Day by day rather than one clever query: the per-day recompute already
    exists and is known to read the same columns the handlers write, and
    reusing it is what keeps the check and the thing being checked from
    drifting apart on their own.
    """
    drifted: list[dict[str, object]] = []
    current = start_date
    while current <= end_date:
        differences = reconcile_date(db, current)
        if differences:
            drifted.append({"date": current, "fields": differences})
        current += timedelta(days=1)
    return drifted


def repair_date(db: Session, target_date: date) -> dict[str, float]:
    """Overwrite one day's stored row with what the raw tables say.

    The only function here that writes, and nothing calls it automatically.
    Repair is a decision someone makes after reading a drift report, not a
    thing that quietly happens -- an aggregate that silently heals itself
    hides the bug that broke it, and the bug is the part worth knowing
    about.

    Recompute-and-overwrite, not an increment: the answer does not depend
    on what was in the row before, so running this twice, or while the
    consumer is also working, converges on the same numbers.
    """
    actual = compute_analytics_for_date(db, target_date)
    statement = insert(AnalyticsDaily).values(date=target_date, **actual)
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[AnalyticsDaily.date], set_=actual
        )
    )
    db.commit()
    return actual

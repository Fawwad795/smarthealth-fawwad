"""Analytics business logic: recomputing analytics_daily from raw data.

Called by the Week 3 Celery rollup task and, later, by task 3.7's
reconciliation check -- both need exactly this "what does the raw data
actually say" computation, just for different reasons.
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
    Slot,
    Visit,
)
from app.models.enums import AppointmentStatus, VisitStatus


def rollup_analytics_for_date(db: Session, target_date: date) -> None:
    """Recompute one day's analytics_daily row from the raw tables and overwrite it.

    A full recompute, not an increment -- see app/workers/tasks/analytics.py
    for why that's the point. INSERT ... ON CONFLICT DO UPDATE is the same
    single-statement reasoning as the slot reservation's atomic UPDATE,
    applied to a row instead of a status: no separate SELECT to decide
    whether to insert or update.
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

    avg_wait_seconds = db.execute(
        select(func.avg(func.extract("epoch", Visit.checked_in_at - Slot.start_time)))
        .select_from(Visit)
        .join(Appointment, Visit.appointment_id == Appointment.id)
        .join(Slot, Appointment.slot_id == Slot.id)
        .where(Visit.checked_in_at >= day_start, Visit.checked_in_at < day_end)
    ).scalar_one()

    failed_jobs_count = db.execute(
        select(func.count())
        .select_from(FailedJob)
        .where(FailedJob.created_at >= day_start, FailedJob.created_at < day_end)
    ).scalar_one()

    stmt = insert(AnalyticsDaily).values(
        date=target_date,
        appointments_booked=appointments_booked,
        completed_visits=completed_visits,
        cancellations=cancellations,
        avg_wait_seconds=avg_wait_seconds,
        failed_jobs_count=failed_jobs_count,
    )

    stmt = stmt.on_conflict_do_update(
        index_elements=[AnalyticsDaily.date],
        set_={
            "appointments_booked": stmt.excluded.appointments_booked,
            "completed_visits": stmt.excluded.completed_visits,
            "cancellations": stmt.excluded.cancellations,
            "avg_wait_seconds": stmt.excluded.avg_wait_seconds,
            "failed_jobs_count": stmt.excluded.failed_jobs_count,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)
    db.commit()

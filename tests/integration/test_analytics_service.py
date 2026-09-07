"""Tests for app/services/analytics.py: the rollup's arithmetic against
known raw data, and that re-running it overwrites rather than accumulates.
"""

from datetime import UTC, date, datetime

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
from app.services.analytics import rollup_analytics_for_date

TARGET_DATE = date(2026, 6, 15)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 15, hour, minute, tzinfo=UTC)


def test_rollup_computes_each_metric_from_raw_data(
    db_session: Session, appointment: Appointment, slot: Slot
) -> None:
    appointment.booked_at = _at(9)

    slot.start_time = _at(8, 0)
    slot.end_time = _at(8, 30)

    db_session.add(
        AppointmentStatusHistory(
            appointment_id=appointment.id,
            from_status=AppointmentStatus.SLOT_RESERVED,
            to_status=AppointmentStatus.CANCELLED,
            actor="PATIENT",
            created_at=_at(10),
        )
    )
    db_session.add(
        Visit(
            appointment_id=appointment.id,
            status=VisitStatus.COMPLETED,
            checked_in_at=_at(8, 15),  # 15 minutes after the slot's 08:00 start
            completed_at=_at(9, 0),
        )
    )
    db_session.add(
        FailedJob(
            job_type="app.workers.tasks.reminders.send_appointment_reminder",
            payload={},
            error="boom",
            attempts=5,
            created_at=_at(8),
        )
    )
    db_session.flush()

    rollup_analytics_for_date(db_session, TARGET_DATE)

    row = db_session.get(AnalyticsDaily, TARGET_DATE)
    assert row.appointments_booked == 1
    assert row.completed_visits == 1
    assert row.cancellations == 1
    assert row.failed_jobs_count == 1
    assert row.avg_wait_seconds == 900.0  # 15 minutes, in seconds


def test_rollup_overwrites_a_stale_row_rather_than_accumulating(
    db_session: Session, appointment: Appointment
) -> None:
    """A pre-existing (possibly stale) row must be overwritten, not added
    to -- this is what makes the rollup safe to run redundantly.
    """
    db_session.add(AnalyticsDaily(date=TARGET_DATE, appointments_booked=999))
    db_session.flush()

    appointment.booked_at = _at(9)
    db_session.flush()

    rollup_analytics_for_date(db_session, TARGET_DATE)

    row = db_session.get(AnalyticsDaily, TARGET_DATE)
    assert row.appointments_booked == 1


def test_rollup_handles_a_day_with_no_data(db_session: Session) -> None:
    """An empty day must not crash -- avg_wait_seconds in particular has to
    stay null rather than erroring on an average of nothing.
    """
    empty_date = date(2020, 1, 1)

    rollup_analytics_for_date(db_session, empty_date)

    row = db_session.get(AnalyticsDaily, empty_date)
    assert row.appointments_booked == 0
    assert row.avg_wait_seconds is None

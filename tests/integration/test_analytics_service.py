"""app/services/analytics.py: the raw-truth computation and the incremental writer.

The two have opposite jobs and are tested for opposite things.
compute_analytics_for_date() must report what the raw tables say and
write nothing -- it is the reconciliation check, and a check that writes
cannot find drift. increment_daily() must add rather than replace, since
it is called once per event.
"""

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AnalyticsDaily,
    Appointment,
    AppointmentStatusHistory,
    Slot,
    Visit,
)
from app.models.enums import AppointmentStatus, VisitStatus
from app.services.analytics import (
    appointments_series,
    compute_analytics_for_date,
    increment_daily,
    summarise_range,
)

TARGET_DATE = date(2026, 6, 15)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 15, hour, minute, tzinfo=UTC)


def test_compute_reports_each_metric_from_the_raw_tables(
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
    db_session.flush()

    assert compute_analytics_for_date(db_session, TARGET_DATE) == {
        "appointments_booked": 1,
        "completed_visits": 1,
        "cancellations": 1,
        "wait_seconds_total": 900.0,  # 15 minutes, in seconds
        "wait_count": 1,
    }


def test_compute_writes_nothing(db_session: Session, appointment: Appointment) -> None:
    """The reason this function stopped being the rollup.

    Until Day 3 it recomputed and overwrote the row. That fought the
    consumer's increments, and it made a reconciliation check vacuous --
    comparing the aggregates against the query that had just written
    them could never find drift. This test is what keeps the write out.
    """
    appointment.booked_at = _at(9)
    db_session.flush()

    compute_analytics_for_date(db_session, TARGET_DATE)

    assert db_session.execute(select(AnalyticsDaily)).scalars().all() == []


def test_compute_handles_a_day_with_no_data(db_session: Session) -> None:
    """An empty day must report zeros rather than erroring or returning None.

    wait_seconds_total in particular: SUM over no rows is NULL in SQL, and
    a None here would propagate into the drift comparison as a difference
    rather than as "nothing happened that day".
    """
    assert compute_analytics_for_date(db_session, date(2020, 1, 1)) == {
        "appointments_booked": 0,
        "completed_visits": 0,
        "cancellations": 0,
        "wait_seconds_total": 0.0,
        "wait_count": 0,
    }


def test_increment_daily_creates_the_row_it_needs(db_session: Session) -> None:
    """The first event of a day has no row to add to."""
    increment_daily(db_session, TARGET_DATE, appointments_booked=1)
    db_session.flush()

    row = db_session.get(AnalyticsDaily, TARGET_DATE)
    assert row.appointments_booked == 1


def test_increment_daily_adds_rather_than_replacing(db_session: Session) -> None:
    """The difference between this and the rollup it replaced.

    Called once per event, so a second call must reach 2. Replacing --
    which is what ON CONFLICT DO UPDATE does by default, and what the old
    rollup deliberately did -- would leave every day stuck at 1.
    """
    increment_daily(db_session, TARGET_DATE, appointments_booked=1)
    increment_daily(db_session, TARGET_DATE, appointments_booked=1)
    db_session.flush()

    assert db_session.get(AnalyticsDaily, TARGET_DATE).appointments_booked == 2


def test_increment_daily_leaves_columns_it_was_not_given_alone(
    db_session: Session,
) -> None:
    """A visit.completed event must not zero the day's booking count.

    Each handler passes only the columns its own event moves, so an
    unnamed column has to keep its value. Building the SET clause from
    the deltas rather than from every column is what makes that true.
    """
    increment_daily(db_session, TARGET_DATE, appointments_booked=5)
    increment_daily(db_session, TARGET_DATE, completed_visits=1)
    db_session.flush()

    row = db_session.get(AnalyticsDaily, TARGET_DATE)
    assert row.appointments_booked == 5
    assert row.completed_visits == 1


def test_summarise_range_reads_only_the_days_asked_for(db_session: Session) -> None:
    """The range is a filter, not a suggestion.

    A summary that quietly included every day would look right on a fresh
    database and wrong on a real one -- the failure would only appear
    once there was history either side of the range.
    """
    increment_daily(db_session, date(2026, 6, 14), appointments_booked=100)
    increment_daily(db_session, TARGET_DATE, appointments_booked=2)
    increment_daily(db_session, date(2026, 6, 16), appointments_booked=100)
    db_session.flush()

    summary = summarise_range(db_session, TARGET_DATE, TARGET_DATE)

    assert summary["appointments_booked"] == 2


def test_summarise_range_divides_the_two_wait_components(
    db_session: Session,
) -> None:
    """The average is computed here, on read, from the stored total and
    count -- which is the whole reason the row stores them separately.
    """
    increment_daily(db_session, TARGET_DATE, wait_seconds_total=900.0, wait_count=1)
    increment_daily(db_session, TARGET_DATE, wait_seconds_total=300.0, wait_count=1)
    db_session.flush()

    summary = summarise_range(db_session, TARGET_DATE, TARGET_DATE)

    assert summary["avg_wait_seconds"] == 600.0
    assert summary["cancellation_rate"] is None


def test_rates_are_null_rather_than_zero_when_there_is_nothing_to_divide(
    db_session: Session,
) -> None:
    """Zero and "no data" are different facts.

    A month with no bookings did not have a 0% cancellation rate, and a
    dashboard drawing 0% would be stating something false rather than
    admitting it has nothing to show.
    """
    summary = summarise_range(db_session, TARGET_DATE, TARGET_DATE)

    assert summary["cancellation_rate"] is None
    assert summary["avg_wait_seconds"] is None
    assert summary["appointments_booked"] == 0


def test_the_series_returns_zero_for_days_with_no_row(db_session: Session) -> None:
    """increment_daily only creates a row when something happens, so a
    quiet day is simply absent from the table. The series fills it in:
    a chart with holes is a worse answer than one with zeros.
    """
    increment_daily(db_session, date(2026, 6, 16), appointments_booked=3)
    db_session.flush()

    series = appointments_series(db_session, date(2026, 6, 15), date(2026, 6, 17))

    assert series == [
        {"date": date(2026, 6, 15), "appointments_booked": 0},
        {"date": date(2026, 6, 16), "appointments_booked": 3},
        {"date": date(2026, 6, 17), "appointments_booked": 0},
    ]

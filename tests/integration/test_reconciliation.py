"""Reconciliation: does the check actually catch a wrong number?

The point of these tests is the failing case. A reconciliation check that
has only ever been observed passing is indistinguishable from one that
always returns "fine".
"""

from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.events.handlers import handle_appointment_booked, handle_visit_completed
from app.models import AnalyticsDaily, Appointment, Slot, Visit
from app.models.enums import VisitStatus
from app.services import analytics as analytics_service


def _book_on(db: Session, appointment: Appointment, when: datetime) -> date:
    """Stamp an appointment as booked at a given moment; return its day.

    The shared fixture stops at REQUESTED, and that is correct -- booked_at
    is written when the saga confirms, not when the row is created.
    Reconciliation buckets appointments by booked_at, so a test about
    counting bookings has to say when the booking actually happened rather
    than assume the fixture did it.
    """
    appointment.booked_at = when
    db.flush()
    return when.date()


def test_a_day_with_nothing_in_it_is_in_sync(db_session: Session) -> None:
    """No row and no activity is agreement, not drift."""
    quiet_day = datetime.now(UTC).date() - timedelta(days=400)

    assert analytics_service.reconcile_date(db_session, quiet_day) == {}


def test_a_tampered_aggregate_is_reported(db_session: Session, appointment) -> None:
    """A stored number that does not match the raw tables is drift.

    Both sides are reported, because "they disagree" is not actionable on
    its own -- the operator needs to see which one moved.
    """
    booked_on = _book_on(db_session, appointment, datetime.now(UTC))
    analytics_service.repair_date(db_session, booked_on)

    row = db_session.get(AnalyticsDaily, booked_on)
    row.appointments_booked += 7
    db_session.flush()

    drift = analytics_service.reconcile_date(db_session, booked_on)

    assert "appointments_booked" in drift
    assert drift["appointments_booked"]["stored"] == (
        drift["appointments_booked"]["actual"] + 7
    )


def test_a_missing_row_with_real_activity_is_drift(
    db_session: Session, appointment
) -> None:
    """An absent row is zeros, not "skip me".

    The failure this guards: if a consumer dies before ever writing a day's
    row, treating missing as "nothing to compare" would call the outage
    healthy.
    """
    booked_on = _book_on(db_session, appointment, datetime.now(UTC))

    drift = analytics_service.reconcile_date(db_session, booked_on)

    assert drift["appointments_booked"]["stored"] == 0.0
    assert drift["appointments_booked"]["actual"] >= 1.0


def test_reconciling_does_not_change_anything(db_session: Session, appointment) -> None:
    """The check is read-only -- the rule the whole design rests on.

    If this ever fails, reconciliation has started repairing what it finds,
    and every future drift report becomes a green light that means nothing.
    """
    booked_on = _book_on(db_session, appointment, datetime.now(UTC))
    analytics_service.repair_date(db_session, booked_on)
    db_session.get(AnalyticsDaily, booked_on).appointments_booked = 99
    db_session.flush()

    analytics_service.reconcile_date(db_session, booked_on)
    analytics_service.reconcile_range(db_session, booked_on, booked_on)

    assert db_session.get(AnalyticsDaily, booked_on).appointments_booked == 99


def test_repair_makes_a_drifted_day_clean(db_session: Session, appointment) -> None:
    """Repair rewrites from raw, and the next check finds nothing."""
    booked_on = _book_on(db_session, appointment, datetime.now(UTC))
    db_session.add(AnalyticsDaily(date=booked_on, appointments_booked=99))
    db_session.flush()

    analytics_service.repair_date(db_session, booked_on)

    assert analytics_service.reconcile_date(db_session, booked_on) == {}


def test_reconciliation_endpoint_is_admin_only(
    client: TestClient, front_desk_auth_headers, admin_auth_headers
) -> None:
    """Front desk may read the clinic's numbers, not the pipeline's health."""
    assert (
        client.get(
            "/api/v1/analytics/reconciliation", headers=front_desk_auth_headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/analytics/reconciliation", headers=admin_auth_headers
        ).status_code
        == 200
    )


# --- the forward direction ------------------------------------------------
# Every test above starts by breaking something and checks the drift is
# reported. None of them proves the *normal* path produces no drift -- and a
# handler and a recompute that were wrong in the same way would agree with
# each other while both being wrong. These two run the real handlers and then
# ask for the drift report.

DAY = date(2026, 6, 15)


def _on(hour: int, minute: int = 0) -> datetime:
    """A moment on DAY. Fixed, so the assertions name one known bucket."""
    return datetime(2026, 6, 15, hour, minute, tzinfo=UTC)


def test_handled_events_leave_the_aggregates_agreeing_with_the_raw_tables(
    db_session: Session, appointment: Appointment, slot: Slot
) -> None:
    """A booking and a completed visit, aggregated by the real handlers."""
    _book_on(db_session, appointment, _on(9))
    slot.start_time = _on(8)
    slot.end_time = _on(8, 30)
    visit = Visit(
        appointment_id=appointment.id,
        status=VisitStatus.COMPLETED,
        checked_in_at=_on(8, 15),
        completed_at=_on(9),
    )
    db_session.add(visit)
    db_session.flush()

    handle_appointment_booked(db_session, {"data": {"appointment_id": appointment.id}})
    handle_visit_completed(db_session, {"data": {"visit_id": visit.id}})
    db_session.flush()

    assert analytics_service.reconcile_date(db_session, DAY) == {}


def test_a_visit_still_in_progress_is_not_reported_as_drift(
    db_session: Session, appointment: Appointment, slot: Slot
) -> None:
    """A patient who has checked in but has not been seen out yet.

    No visit.completed event exists for this visit, so no handler has run
    and nothing has been aggregated. The recompute has to agree. If it
    counts the wait at check-in instead, every clinic with a patient
    mid-visit sees phantom drift for as long as that visit is open --
    which is most of the working day.
    """
    _book_on(db_session, appointment, _on(9))
    slot.start_time = _on(8)
    slot.end_time = _on(8, 30)
    db_session.add(
        Visit(
            appointment_id=appointment.id,
            status=VisitStatus.CHECKED_IN,
            checked_in_at=_on(8, 15),
            completed_at=None,
        )
    )
    db_session.flush()

    handle_appointment_booked(db_session, {"data": {"appointment_id": appointment.id}})
    db_session.flush()

    assert analytics_service.reconcile_date(db_session, DAY) == {}

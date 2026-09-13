"""app/kafka/handlers.py: what each event does to analytics_daily.

The handlers are the only writers of that table, so these tests are the
only thing standing between an event and a wrong dashboard. Each asserts
the day the number lands on as well as the number itself -- a count in
the right total but the wrong bucket is still a lie, and it is the
failure a naive `datetime.now()` would produce when replaying a backlog.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.kafka.consumer import dispatch
from app.kafka.errors import PermanentEventError
from app.kafka.handlers import (
    handle_appointment_booked,
    handle_appointment_cancelled,
    handle_visit_completed,
)
from app.models import (
    AnalyticsDaily,
    Appointment,
    AppointmentStatusHistory,
    Slot,
    Visit,
)
from app.models.enums import AppointmentStatus, VisitStatus

BOOKED_ON = date(2026, 6, 15)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=UTC)


def _envelope(event_type: str, data: dict[str, int]) -> dict[str, object]:
    return {"event_id": "evt-h", "event_type": event_type, "data": data}


def test_a_booking_counts_on_the_day_it_was_booked(
    db_session: Session, appointment: Appointment
) -> None:
    """Bucketed by appointments.booked_at -- the same column the
    reconciliation counts -- not by when the event happened to be consumed.
    """
    appointment.booked_at = _at(15, 9)
    db_session.flush()

    handle_appointment_booked(
        db_session, _envelope("appointment.booked", {"appointment_id": appointment.id})
    )
    db_session.flush()

    assert db_session.get(AnalyticsDaily, BOOKED_ON).appointments_booked == 1


def test_a_cancellation_counts_on_the_day_it_was_recorded(
    db_session: Session, appointment: Appointment
) -> None:
    """Read from the status history, because the appointment itself knows
    only that it is cancelled -- not when it became so.
    """
    db_session.add(
        AppointmentStatusHistory(
            appointment_id=appointment.id,
            from_status=AppointmentStatus.SLOT_RESERVED,
            to_status=AppointmentStatus.CANCELLED,
            actor="PATIENT",
            created_at=_at(15, 10),
        )
    )
    db_session.flush()

    handle_appointment_cancelled(
        db_session,
        _envelope("appointment.cancelled", {"appointment_id": appointment.id}),
    )
    db_session.flush()

    assert db_session.get(AnalyticsDaily, BOOKED_ON).cancellations == 1


def test_a_completed_visit_records_its_wait_as_a_total_and_a_count(
    db_session: Session, appointment: Appointment, slot: Slot
) -> None:
    """The two components, not an average.

    900 seconds over one visit is the same stored state as 900 over one
    visit -- the division happens when the endpoint is asked, which is
    what makes a second visit foldable into it at all.
    """
    slot.start_time = _at(15, 8)
    slot.end_time = _at(15, 8, 30)
    db_session.add(
        Visit(
            appointment_id=appointment.id,
            status=VisitStatus.COMPLETED,
            checked_in_at=_at(15, 8, 15),
            completed_at=_at(15, 9),
        )
    )
    db_session.flush()

    visit_id = db_session.query(Visit).one().id
    handle_visit_completed(
        db_session, _envelope("visit.completed", {"visit_id": visit_id})
    )
    db_session.flush()

    row = db_session.get(AnalyticsDaily, BOOKED_ON)
    assert row.completed_visits == 1
    assert row.wait_seconds_total == 900.0
    assert row.wait_count == 1


def test_a_visit_spanning_midnight_splits_across_two_days(
    db_session: Session, appointment: Appointment, slot: Slot
) -> None:
    """The completion and the wait belong to different days on purpose.

    A visit is completed on the day it completed; its wait belongs to the
    day the patient checked in. Bucketing both by one date would be
    simpler and would disagree with the reconciliation, which buckets
    them separately -- reported as drift that was never real.
    """
    slot.start_time = _at(15, 23)
    slot.end_time = _at(15, 23, 30)
    db_session.add(
        Visit(
            appointment_id=appointment.id,
            status=VisitStatus.COMPLETED,
            checked_in_at=_at(15, 23, 15),
            completed_at=_at(16, 0, 30),  # just after midnight
        )
    )
    db_session.flush()

    visit_id = db_session.query(Visit).one().id
    handle_visit_completed(
        db_session, _envelope("visit.completed", {"visit_id": visit_id})
    )
    db_session.flush()

    check_in_day = db_session.get(AnalyticsDaily, date(2026, 6, 15))
    completion_day = db_session.get(AnalyticsDaily, date(2026, 6, 16))

    assert check_in_day.wait_count == 1
    assert check_in_day.completed_visits == 0
    assert completion_day.completed_visits == 1
    assert completion_day.wait_count == 0


def test_an_early_arrival_records_a_negative_wait(
    db_session: Session, appointment: Appointment, slot: Slot
) -> None:
    """Not clamped to zero. A patient arriving ten minutes early waited
    minus ten minutes, and flooring that would bias the average upwards
    while looking like tidying up.
    """
    slot.start_time = _at(15, 9)
    slot.end_time = _at(15, 9, 30)
    db_session.add(
        Visit(
            appointment_id=appointment.id,
            status=VisitStatus.COMPLETED,
            checked_in_at=_at(15, 8, 50),  # ten minutes early
            completed_at=_at(15, 9, 30),
        )
    )
    db_session.flush()

    visit_id = db_session.query(Visit).one().id
    handle_visit_completed(
        db_session, _envelope("visit.completed", {"visit_id": visit_id})
    )
    db_session.flush()

    assert db_session.get(AnalyticsDaily, BOOKED_ON).wait_seconds_total == -600.0


def test_an_event_for_a_row_that_does_not_exist_is_permanent(
    db_session: Session,
) -> None:
    """Not transient, and the distinction decides whether the offset moves.

    The event is published only after the transaction that created the
    appointment committed, and every FK is ON DELETE RESTRICT, so the row
    cannot arrive late and cannot have been deleted. Retrying forever
    would block the partition on an anomaly no retry can fix.
    """
    with pytest.raises(PermanentEventError, match="does not exist"):
        handle_appointment_booked(
            db_session, _envelope("appointment.booked", {"appointment_id": 999_999})
        )


def test_dispatch_routes_an_event_to_its_handler(
    db_session: Session, appointment: Appointment
) -> None:
    """The seam between the loop and the handlers.

    Every test in test_consumer_loop.py monkeypatches dispatch to watch
    what the loop does with it, which leaves the routing itself
    unexercised -- the registry lookup could be wired to the wrong
    handler and the whole suite would still pass.
    """
    appointment.booked_at = _at(15, 9)
    db_session.flush()

    dispatch(
        db_session, _envelope("appointment.booked", {"appointment_id": appointment.id})
    )
    db_session.flush()

    assert db_session.get(AnalyticsDaily, BOOKED_ON).appointments_booked == 1


def test_dispatch_ignores_an_event_nothing_handles(db_session: Session) -> None:
    """Not every event is this consumer's business.

    Topics are per aggregate, so appointment.confirmed arrives here
    whether or not anything acts on it. Treating that as an error would
    dead-letter perfectly good messages for not being about analytics.
    """
    dispatch(db_session, _envelope("appointment.confirmed", {"appointment_id": 1}))

    assert db_session.get(AnalyticsDaily, BOOKED_ON) is None

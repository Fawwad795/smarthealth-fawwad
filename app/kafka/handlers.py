"""What each event does to the analytics aggregates.

Every handler reads its bucket date from the same raw column the
reconciliation check reads -- appointments.booked_at, the CANCELLED row's
created_at, visits.completed_at -- rather than from the envelope's
occurred_at. The two are within microseconds of each other, but only
"within microseconds" apart, and a booking committed either side of
midnight would land in different days for the aggregate and the
reconciliation. Reading the same column makes that class of drift
structurally impossible instead of merely unlikely.

Handlers are not limited to the payload. Events carry ids precisely so a
consumer can look up whatever else it needs.
"""

from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events.envelope import EventType
from app.kafka.errors import PermanentEventError
from app.models import Appointment, AppointmentStatusHistory, Slot, Visit
from app.models.enums import AppointmentStatus
from app.services.analytics import increment_daily


def handle_appointment_confirmed(db: Session, envelope: dict[str, Any]) -> None:
    """appointments_booked +1, on the day the appointment was booked.

    Driven by appointment.confirmed, not appointment.booked. booked is
    queued while the appointment is still REQUESTED, and booked_at is only
    written later, by the saga's confirm Activity -- so a booked event can
    arrive before there is a day to count it on. Day 5's crash demo froze
    the sagas and every booking event was dead-lettered that way.
    confirmed is recorded in the same transaction that sets booked_at, so
    when it arrives the column is guaranteed to be filled.
    """
    appointment_id = envelope["data"]["appointment_id"]
    row = db.execute(
        select(Appointment.booked_at).where(Appointment.id == appointment_id)
    ).one_or_none()

    # Both branches are permanent, and kept apart so the dead-letter says
    # which anomaly it was. A missing row cannot arrive late: the event is
    # published only after its transaction committed, and every FK is
    # ON DELETE RESTRICT. An empty booked_at cannot fill in later either:
    # confirm writes it in the same transaction as this event, and nothing
    # ever clears it. Collapsing the two into one None is how Day 5's
    # dead-letters came to say "does not exist" about a row that existed.
    if row is None:
        raise PermanentEventError(f"appointment {appointment_id} does not exist")
    if row.booked_at is None:
        raise PermanentEventError(
            f"appointment {appointment_id} is confirmed but has no booked_at"
        )

    increment_daily(db, row.booked_at.date(), appointments_booked=1)


def handle_appointment_cancelled(db: Session, envelope: dict[str, Any]) -> None:
    """cancellations +1, on the day the cancellation was recorded.

    Counted from appointment_status_history rather than from the
    appointment's own status, because the appointment carries only its
    current state -- it cannot say *when* it became CANCELLED, and a
    daily bucket needs exactly that.
    """
    appointment_id = envelope["data"]["appointment_id"]
    cancelled_at = db.execute(
        select(AppointmentStatusHistory.created_at)
        .where(
            AppointmentStatusHistory.appointment_id == appointment_id,
            AppointmentStatusHistory.to_status == AppointmentStatus.CANCELLED,
        )
        .order_by(AppointmentStatusHistory.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if cancelled_at is None:
        raise PermanentEventError(
            f"appointment {appointment_id} has no CANCELLED history row"
        )

    increment_daily(db, cancelled_at.date(), cancellations=1)


def handle_visit_completed(db: Session, envelope: dict[str, Any]) -> None:
    """completed_visits +1, plus this visit's contribution to the wait time.

    Two increments, and they can land on different days on purpose: a
    visit is counted as completed on the day it completed, while its wait
    belongs to the day the patient checked in. A visit spanning midnight
    therefore touches two rows -- which is what the reconciliation
    expects, because that is how the raw query buckets them.
    """
    visit_id = envelope["data"]["visit_id"]
    row = db.execute(
        select(Visit.completed_at, Visit.checked_in_at, Slot.start_time)
        .join(Appointment, Visit.appointment_id == Appointment.id)
        .join(Slot, Appointment.slot_id == Slot.id)
        .where(Visit.id == visit_id)
    ).one_or_none()

    if row is None:
        raise PermanentEventError(f"visit {visit_id} does not exist")

    completed_at, checked_in_at, slot_start = row
    if completed_at is None:
        raise PermanentEventError(f"visit {visit_id} is not completed")

    increment_daily(db, completed_at.date(), completed_visits=1)
    increment_daily(
        db,
        checked_in_at.date(),
        # Negative when a patient arrives early, and deliberately so: an
        # early arrival is a negative wait, and clamping it to zero would
        # bias the average upwards while looking like tidying up.
        wait_seconds_total=(checked_in_at - slot_start).total_seconds(),
        wait_count=1,
    )


# The three events that move a number. The other three -- booked,
# published, billing.updated -- are legitimately received and ignored;
# topics are per aggregate, so this consumer sees more than it acts on.
# booked is ignored on purpose: handle_appointment_confirmed says why
# confirmed drives the count instead.
HANDLERS: dict[EventType, Callable[[Session, dict[str, Any]], None]] = {
    EventType.APPOINTMENT_CONFIRMED: handle_appointment_confirmed,
    EventType.APPOINTMENT_CANCELLED: handle_appointment_cancelled,
    EventType.VISIT_COMPLETED: handle_visit_completed,
}

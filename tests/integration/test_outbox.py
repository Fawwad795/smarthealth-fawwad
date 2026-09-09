"""Tests for the outbox: what record_event writes, and what it refuses to do.

The outbox exists to close the gap between committing a business change and
announcing it. Its correctness is therefore almost entirely about
*transaction boundaries*, which is what most of these tests check -- not the
contents of any one row.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import set_correlation_id
from app.events.envelope import EventType
from app.events.outbox import record_event
from app.models import Appointment, OutboxEvent
from app.models.enums import AppointmentStatus
from app.services import visit as visit_service
from app.services.appointment_scheduling import cancel_appointment
from app.services.billing import BillingChecker


def _events(db: Session) -> list[OutboxEvent]:
    """Every queued event, oldest first."""
    return list(
        db.execute(select(OutboxEvent).order_by(OutboxEvent.id)).scalars().all()
    )


def test_record_event_does_not_commit(db_session: Session) -> None:
    """The single most important property of record_event.

    The caller is mid-transaction, part-way through the change this event
    describes. Committing here would split the two apart again and put back
    exactly the gap the outbox exists to close -- the event would survive a
    rollback of the change that caused it, which is the "announced a lie"
    failure in written form.

    A pending object still sitting in Session.new has not been flushed or
    committed, which is what this asserts.
    """
    event = record_event(
        db_session, EventType.VISIT_COMPLETED, 1, {"visit_id": 1, "appointment_id": 1}
    )

    assert event in db_session.new


def test_a_rolled_back_change_takes_its_event_with_it(db_session: Session) -> None:
    """The other half of the same property, from the outside.

    Because the row is written in the caller's transaction, undoing the
    caller undoes the event. An event for something that never happened is
    the failure mode that "publish, then commit" suffers from.
    """
    db_session.begin_nested()
    record_event(
        db_session, EventType.VISIT_COMPLETED, 1, {"visit_id": 1, "appointment_id": 1}
    )
    db_session.flush()
    assert _events(db_session) != []

    db_session.rollback()

    assert _events(db_session) == []


def test_a_queued_event_starts_unpublished(db_session: Session) -> None:
    """published_at is what the relay claims rows by. A row born with a
    timestamp would never be sent at all."""
    record_event(
        db_session, EventType.VISIT_COMPLETED, 1, {"visit_id": 1, "appointment_id": 1}
    )
    db_session.flush()

    assert _events(db_session)[0].published_at is None


def test_a_queued_event_captures_the_current_correlation_id(
    db_session: Session,
) -> None:
    """Read from ambient context rather than passed in, so an event carries
    the id of the request that caused it without every emit point having to
    remember to thread it through."""
    set_correlation_id("req-outbox-test")

    record_event(
        db_session, EventType.VISIT_COMPLETED, 1, {"visit_id": 1, "appointment_id": 1}
    )
    db_session.flush()

    assert _events(db_session)[0].correlation_id == "req-outbox-test"


def test_completing_a_visit_queues_exactly_one_event(
    db_session: Session, appointment: Appointment
) -> None:
    """One business change, one event -- not zero, and not two."""
    appointment.status = AppointmentStatus.CONFIRMED
    db_session.flush()
    visit_service.check_in(db_session, appointment.id)
    visit_service.start_visit(db_session, appointment.id)

    visit_service.complete_visit(db_session, appointment.id, actor="PROVIDER")

    completed = [
        e for e in _events(db_session) if e.event_type == EventType.VISIT_COMPLETED
    ]
    assert len(completed) == 1
    assert completed[0].data["appointment_id"] == appointment.id


def test_an_idempotent_repeat_does_not_queue_a_second_event(
    db_session: Session, appointment: Appointment
) -> None:
    """complete_visit returns early for an already-completed visit. The
    event must return early with it -- a duplicate here would inflate the
    analytics the consumer maintains, which is the exact problem ("the
    dashboard disagrees with the table") this project exists to fix.
    """
    appointment.status = AppointmentStatus.CONFIRMED
    db_session.flush()
    visit_service.check_in(db_session, appointment.id)
    visit_service.start_visit(db_session, appointment.id)
    visit_service.complete_visit(db_session, appointment.id, actor="PROVIDER")

    visit_service.complete_visit(db_session, appointment.id, actor="PROVIDER")

    completed = [
        e for e in _events(db_session) if e.event_type == EventType.VISIT_COMPLETED
    ]
    assert len(completed) == 1


def test_cancelling_queues_a_cancelled_event(
    db_session: Session, appointment: Appointment
) -> None:
    cancel_appointment(db_session, appointment.id, actor="PATIENT")

    cancelled = [
        e
        for e in _events(db_session)
        if e.event_type == EventType.APPOINTMENT_CANCELLED
    ]
    assert len(cancelled) == 1
    assert cancelled[0].aggregate_id == appointment.id


def test_a_billing_precheck_queues_one_event(
    db_session: Session, appointment: Appointment
) -> None:
    BillingChecker().precheck(db_session, appointment, appointment.idempotency_key)

    updated = [
        e for e in _events(db_session) if e.event_type == EventType.BILLING_UPDATED
    ]
    assert len(updated) == 1


def test_every_queued_event_carries_ids_only(
    db_session: Session, appointment: Appointment
) -> None:
    """The PHI rule, enforced rather than remembered.

    Rule 6.6 says events carry ids and never names, contacts or patient
    text. Ids are integers, so a non-integer value in `data` is either PHI
    or on its way to becoming PHI the next time someone adds a field. This
    exercises several unrelated emit points and holds all of them to it, so
    a future emit point that slips a patient name in fails here rather than
    at a mentor's review.
    """
    appointment.status = AppointmentStatus.CONFIRMED
    db_session.flush()
    BillingChecker().precheck(db_session, appointment, appointment.idempotency_key)
    visit_service.check_in(db_session, appointment.id)
    visit_service.start_visit(db_session, appointment.id)
    visit_service.complete_visit(db_session, appointment.id, actor="PROVIDER")

    events = _events(db_session)
    assert events, "no events queued -- the assertion below would pass vacuously"
    for event in events:
        for field, value in event.data.items():
            assert isinstance(value, int), (
                f"{event.event_type}.data[{field!r}] is {type(value).__name__}, "
                "not an id -- events carry ids only (rule 6.6)"
            )

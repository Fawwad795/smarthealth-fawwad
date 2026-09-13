"""Replaying an event must not move a number.

The Definition of Done asks for this one specifically, and it is the
guarantee the whole consumer design exists to provide: Kafka delivers
at-least-once, and the outbox relay republishes any batch it failed
part-way through, so the same event_id arriving twice is routine rather
than exceptional.

Two neighbouring test modules each prove half of it. test_dedupe.py
proves the claim row rejects a second insert; test_consumer_loop.py
proves a duplicate delivery is skipped and still advances the offset.
Neither one reads analytics_daily afterwards -- and that row is the
number a dashboard would show. This module drives one real
visit.completed envelope through the real loop twice, with the real
dispatch and the real handler, and asserts the count stayed at 1.
"""

import json
import threading
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kafka import consumer as consumer_module
from app.models import AnalyticsDaily, Appointment, ProcessedEvent, Slot, Visit
from app.models.enums import VisitStatus
from tests.kafka_fakes import FakeConsumer, FakeMessage

VISIT_DAY = date(2026, 6, 15)


def _at(hour: int, minute: int = 0) -> datetime:
    """A moment on VISIT_DAY, so every assertion names one known bucket."""
    return datetime(2026, 6, 15, hour, minute, tzinfo=UTC)


def _completed_visit(db: Session, appointment: Appointment, slot: Slot) -> int:
    """A visit that checked in 15 minutes late and completed the same day.

    Fixed times rather than now(): the handler buckets by the visit's own
    columns, so a test that used the current clock would assert against a
    different row each day and would straddle midnight once a year.
    """
    slot.start_time = _at(8)
    slot.end_time = _at(8, 30)
    visit = Visit(
        appointment_id=appointment.id,
        status=VisitStatus.COMPLETED,
        checked_in_at=_at(8, 15),
        completed_at=_at(9),
    )
    db.add(visit)
    db.flush()
    return visit.id


def _envelope(visit_id: int) -> dict[str, object]:
    """One visit.completed envelope, in the shape the producer emits."""
    return {
        "event_id": "evt-replay-1",
        "event_type": "visit.completed",
        "version": 1,
        "occurred_at": "2026-06-15T09:00:00+00:00",
        "correlation_id": "req-replay-1",
        "data": {"visit_id": visit_id},
    }


def _deliver(envelopes: list[tuple[dict[str, object], int]]) -> FakeConsumer:
    """Run the real loop over these (envelope, offset) pairs until it stops."""
    stop = threading.Event()
    fake = FakeConsumer(
        [
            FakeMessage(json.dumps(envelope).encode(), offset=offset)
            for envelope, offset in envelopes
        ],
        stop,
    )
    consumer_module.consume_forever(fake, stop)
    return fake


def test_the_same_event_delivered_twice_does_not_move_the_count(
    db_session: Session, worker_session: None, appointment: Appointment, slot: Slot
) -> None:
    """The whole point, end to end: two deliveries, one increment.

    Nothing here is stubbed between the message and the aggregate -- the
    loop claims the event, dispatches to the real handler, and the handler
    writes the real analytics_daily row. A regression anywhere along that
    path shows up as completed_visits == 2.
    """
    visit_id = _completed_visit(db_session, appointment, slot)
    envelope = _envelope(visit_id)

    fake = _deliver([(envelope, 11), (envelope, 12)])

    row = db_session.get(AnalyticsDaily, VISIT_DAY)
    assert row.completed_visits == 1
    assert row.wait_count == 1
    assert row.wait_seconds_total == 900.0

    # The second delivery is a skip, not a failure: its offset still has to
    # move, or the consumer would re-read that duplicate forever.
    assert fake.committed == [11, 12]
    assert fake.seeks == []


def test_only_one_claim_is_recorded_for_the_two_deliveries(
    db_session: Session, worker_session: None, appointment: Appointment, slot: Slot
) -> None:
    """The claim row is what made the second delivery a no-op.

    Asserted separately from the count above so a failure says which half
    broke: a missing claim row means the guard never ran, while a claim
    row beside a doubled count would mean the claim and the handler stopped
    sharing a transaction.
    """
    visit_id = _completed_visit(db_session, appointment, slot)
    envelope = _envelope(visit_id)

    _deliver([(envelope, 11), (envelope, 12)])

    claims = (
        db_session.execute(
            select(ProcessedEvent).where(ProcessedEvent.event_id == "evt-replay-1")
        )
        .scalars()
        .all()
    )
    assert len(claims) == 1

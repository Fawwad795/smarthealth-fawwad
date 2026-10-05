"""Tests for the relay: the half of the outbox pattern that talks to Kafka.

No broker is involved. app.events.relay.publish is replaced with a spy,
which works because relay.py looks the name up in its own module globals at
call time -- the same seam app/db/session.py relies on.

What matters here is not that a message is formatted correctly but *when
published_at is written*. Stamping a row whose message never arrived would
lose the event permanently, which is the one failure the outbox exists to
make impossible.
"""

from typing import Any

import pytest
from confluent_kafka import KafkaException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import relay
from app.events.envelope import EventType
from app.events.outbox import record_event
from app.models import OutboxEvent


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Replace the producer with a spy, and hand back what it was given."""
    captured: list[tuple[str, str, dict[str, Any]]] = []

    def spy(topic: str, key: str, value: dict[str, Any]) -> None:
        captured.append((topic, key, value))

    monkeypatch.setattr(relay, "publish", spy)
    return captured


@pytest.fixture()
def failing_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the producer with one that never acknowledges."""

    def boom(topic: str, key: str, value: dict[str, Any]) -> None:
        raise KafkaException("broker unreachable")

    monkeypatch.setattr(relay, "publish", boom)


def _queue(db: Session, event_type: EventType, aggregate_id: int) -> None:
    """Queue an event the way production does -- committed.

    commit() rather than flush() matters for the failure tests below. By
    the time the relay sees a row, the business transaction that wrote it
    has long since committed; a merely-flushed row would vanish on the
    rollback those tests perform, and they would then be asserting against
    an empty table rather than a surviving unpublished one.
    """
    record_event(db, event_type, aggregate_id, {"appointment_id": aggregate_id})
    db.commit()


def _pending(db: Session) -> list[OutboxEvent]:
    return list(
        db.execute(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.id)
        )
        .scalars()
        .all()
    )


def test_publishing_marks_the_rows(
    db_session: Session, sent: list[tuple[str, str, dict[str, Any]]]
) -> None:
    _queue(db_session, EventType.APPOINTMENT_BOOKED, 1)
    _queue(db_session, EventType.APPOINTMENT_CONFIRMED, 1)

    count = relay.publish_pending_events(db_session)

    assert count == 2
    assert len(sent) == 2
    assert _pending(db_session) == []


def test_a_failed_publish_leaves_the_row_unpublished(
    db_session: Session, failing_publish: None
) -> None:
    """The single most important test in this file.

    If published_at were stamped before the broker acknowledged, a message
    that never arrived would be recorded as sent and nothing would ever
    retry it -- the event lost silently, which is exactly the failure the
    outbox was built to prevent. The row must survive the failure still
    marked pending, so the next run picks it up.
    """
    _queue(db_session, EventType.APPOINTMENT_BOOKED, 1)

    with pytest.raises(KafkaException):
        relay.publish_pending_events(db_session)

    db_session.rollback()
    assert len(_pending(db_session)) == 1


def test_a_partial_failure_marks_nothing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch is all-or-nothing: the second message failing rolls back the
    first one's mark too, so both are sent again next run.

    That re-sends a message which already arrived, and that is deliberate.
    At-least-once with duplicates is the accepted trade -- the consumer's
    processed_events table absorbs them (task 3.5). The alternative,
    marking rows before knowing their fate, loses events instead, and a
    lost event cannot be recovered by anything downstream.
    """
    calls: list[str] = []

    def fail_on_second(topic: str, key: str, value: dict[str, Any]) -> None:
        calls.append(key)
        if len(calls) == 2:
            raise KafkaException("broker went away mid-batch")

    monkeypatch.setattr(relay, "publish", fail_on_second)
    _queue(db_session, EventType.APPOINTMENT_BOOKED, 1)
    _queue(db_session, EventType.APPOINTMENT_CONFIRMED, 2)

    with pytest.raises(KafkaException):
        relay.publish_pending_events(db_session)

    db_session.rollback()
    assert len(_pending(db_session)) == 2


def test_already_published_rows_are_not_sent_again(
    db_session: Session, sent: list[tuple[str, str, dict[str, Any]]]
) -> None:
    _queue(db_session, EventType.APPOINTMENT_BOOKED, 1)
    relay.publish_pending_events(db_session)

    relay.publish_pending_events(db_session)

    assert len(sent) == 1


def test_events_are_published_oldest_first(
    db_session: Session, sent: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """A consumer must see `booked` before `confirmed`. Ordering by id is
    what makes the queue a queue rather than a bag."""
    _queue(db_session, EventType.APPOINTMENT_BOOKED, 1)
    _queue(db_session, EventType.APPOINTMENT_CONFIRMED, 1)
    _queue(db_session, EventType.APPOINTMENT_CANCELLED, 1)

    relay.publish_pending_events(db_session)

    assert [value["event_type"] for _, _, value in sent] == [
        "appointment.booked",
        "appointment.confirmed",
        "appointment.cancelled",
    ]


def test_the_batch_limit_is_respected(
    db_session: Session, sent: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """A backlog drains over several runs rather than in one transaction
    holding locks on every row in the table."""
    for _ in range(5):
        _queue(db_session, EventType.APPOINTMENT_BOOKED, 1)

    count = relay.publish_pending_events(db_session, limit=2)

    assert count == 2
    assert len(_pending(db_session)) == 3


def test_the_message_is_keyed_by_its_aggregate(
    db_session: Session, sent: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """The key decides the partition, and the partition is the only thing
    Kafka orders within. Keying by appointment id is what guarantees one
    appointment's events stay in sequence."""
    _queue(db_session, EventType.APPOINTMENT_BOOKED, 42)

    relay.publish_pending_events(db_session)

    topic, key, _ = sent[0]
    assert topic == "app.appointments"
    assert key == "42"


def test_the_envelope_carries_every_required_field(
    db_session: Session, sent: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """The envelope is a contract other processes parse. A missing or
    renamed field breaks a consumer that cannot be changed in the same
    deploy."""
    _queue(db_session, EventType.APPOINTMENT_BOOKED, 1)

    relay.publish_pending_events(db_session)

    _, _, envelope = sent[0]
    assert set(envelope) == {
        "event_id",
        "event_type",
        "version",
        "occurred_at",
        "correlation_id",
        "data",
    }
    assert envelope["version"] == 1
    assert envelope["occurred_at"].endswith("+00:00")

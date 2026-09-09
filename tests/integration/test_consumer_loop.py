"""The consumer loop's offset policy: when does the bookmark move?

This is the consumer's entire correctness story. A Kafka offset is a
per-partition position, not a per-message acknowledgement, so committing
it is the irreversible act of declaring everything up to here handled.
Each test below drives the real loop with a fake broker and asserts on the
two calls that decide the outcome: commit() and seek().
"""

import json
import threading
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_correlation_id
from app.models import FailedJob, ProcessedEvent
from app.workers import consumer as consumer_module
from tests.kafka_fakes import FakeConsumer, FakeMessage

ENVELOPE = {
    "event_id": "evt-loop-1",
    "event_type": "visit.completed",
    "version": 1,
    "occurred_at": "2026-09-09T10:00:00+00:00",
    "correlation_id": "req-loop-1",
    "data": {"visit_id": 7},
}


def _message(payload: object, *, offset: int = 11) -> FakeMessage:
    """A message carrying `payload`, encoded as the producer encodes it."""
    return FakeMessage(json.dumps(payload).encode(), offset=offset)


def _run(messages: list[FakeMessage]) -> FakeConsumer:
    """Drive the real loop over `messages` until it runs out and stops."""
    stop = threading.Event()
    fake = FakeConsumer(messages, stop)
    consumer_module.consume_forever(fake, stop)
    return fake


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the transient-failure pause to zero.

    The two seconds are correct in production -- they stop a database
    outage becoming a hot loop -- and pure dead time in a test.
    """
    monkeypatch.setattr(consumer_module, "RETRY_BACKOFF_SECONDS", 0)


def test_a_processed_message_commits_its_offset(
    monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """The happy path: work first, then move the bookmark past it."""
    handled: list[dict[str, Any]] = []
    monkeypatch.setattr(
        consumer_module, "dispatch", lambda db, envelope: handled.append(envelope)
    )

    fake = _run([_message(ENVELOPE, offset=11)])

    assert handled == [ENVELOPE]
    assert fake.committed == [11]
    assert fake.seeks == []


def test_the_offset_is_committed_only_after_the_handler_returns(
    monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """Ordering, not just the end state.

    A commit that happened before the handler ran would leave the same
    committed==[11] as the test above, while being the exact bug that
    loses an event on a crash. Recording the order is what tells the two
    apart.
    """
    events: list[str] = []
    fake_holder: dict[str, FakeConsumer] = {}

    def spy_dispatch(db: Session, envelope: dict[str, Any]) -> None:
        events.append(f"handled:{len(fake_holder['fake'].committed)}")

    monkeypatch.setattr(consumer_module, "dispatch", spy_dispatch)

    stop = threading.Event()
    fake = FakeConsumer([_message(ENVELOPE, offset=11)], stop)
    fake_holder["fake"] = fake
    consumer_module.consume_forever(fake, stop)

    # The handler ran while nothing had been committed yet.
    assert events == ["handled:0"]
    assert fake.committed == [11]


def test_a_transient_failure_rewinds_and_does_not_commit(
    monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """A database that is briefly down must not cost an event.

    The seek is the part worth pinning: declining to commit is not enough
    on its own, because poll() advances the client's own position anyway,
    so without the rewind this message would be skipped until the next
    restart.
    """

    def boom(db: Session, envelope: dict[str, Any]) -> None:
        raise RuntimeError("database is briefly unreachable")

    monkeypatch.setattr(consumer_module, "dispatch", boom)

    fake = _run([_message(ENVELOPE, offset=11)])

    assert fake.committed == []
    assert fake.seeks == [11]


def test_a_malformed_message_is_dead_lettered_then_committed(
    db_session: Session, worker_session: None
) -> None:
    """The poison-message policy: record it, then step over it.

    Refusing to commit would be defensible for one message and fatal for
    the partition, since every later event sits behind it forever. The
    failed_jobs row is what stops "we moved past it" from meaning
    "nobody will ever know".
    """
    fake = _run([FakeMessage(b"{not json at all", offset=11)])

    assert fake.committed == [11]
    assert fake.seeks == []

    failure = db_session.execute(
        select(FailedJob).where(FailedJob.job_type == "app.workers.consumer")
    ).scalar_one()
    assert failure.payload == {"topic": "app.visits", "partition": 0, "offset": 11}
    assert "not valid JSON" in failure.error
    assert failure.attempts == 1


def test_the_dead_letter_row_records_no_message_body(
    db_session: Session, worker_session: None
) -> None:
    """PHI discipline (rule 6.6) on the one path that handles unknown bytes.

    Our own events carry ids only, but a message that failed to parse is
    by definition of unknown shape. failed_jobs stores coordinates, which
    are enough to find it in Kafka UI, and never the payload itself.
    """
    secret = json.dumps({"patient_name": "Jane Doe", "phone": "555-0100"}).encode()

    _run([FakeMessage(secret, offset=11)])

    failure = db_session.execute(
        select(FailedJob).where(FailedJob.job_type == "app.workers.consumer")
    ).scalar_one()
    stored = json.dumps({"payload": failure.payload, "error": failure.error})
    assert "Jane Doe" not in stored
    assert "555-0100" not in stored


def test_one_bad_message_does_not_stop_the_ones_behind_it(
    monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """The rule the brief states outright: a consumer must never die on one
    bad message. Asserted with a good message queued behind a hopeless one.
    """
    handled: list[str] = []
    monkeypatch.setattr(
        consumer_module,
        "dispatch",
        lambda db, envelope: handled.append(envelope["event_id"]),
    )

    fake = _run(
        [
            FakeMessage(b"{not json at all", offset=11),
            _message(ENVELOPE, offset=12),
        ]
    )

    assert handled == ["evt-loop-1"]
    assert fake.committed == [11, 12]


def test_a_broker_notice_is_skipped_without_touching_offsets(
    worker_session: None,
) -> None:
    """Messages carrying an error() are broker-level notices -- a partition
    EOF, a rebalance -- not our events. They have no payload to process and
    no offset of ours to move.
    """
    fake = _run([FakeMessage(None, offset=11, error="PARTITION_EOF")])

    assert fake.committed == []
    assert fake.seeks == []


def test_the_envelopes_correlation_id_is_set_for_the_handler(
    monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """The success criterion for 3.8: one id greps a booking across API,
    worker and consumer. That only holds if the consumer adopts the id the
    envelope carried rather than minting its own.
    """
    seen: list[str | None] = []
    monkeypatch.setattr(
        consumer_module,
        "dispatch",
        lambda db, envelope: seen.append(get_correlation_id()),
    )

    _run([_message(ENVELOPE, offset=11)])

    assert seen == ["req-loop-1"]


def test_a_duplicate_delivery_is_skipped_but_still_advances_the_offset(
    monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """The same event delivered twice must be handled once and committed twice.

    Handled once is the point of the guard. Committed twice is just as
    necessary: a skip is not a failure, and refusing to move the offset
    past a duplicate would re-read it forever.
    """
    handled: list[str] = []
    monkeypatch.setattr(
        consumer_module,
        "dispatch",
        lambda db, envelope: handled.append(envelope["event_id"]),
    )

    fake = _run([_message(ENVELOPE, offset=11), _message(ENVELOPE, offset=12)])

    assert handled == ["evt-loop-1"]
    assert fake.committed == [11, 12]


def test_a_failed_handler_leaves_the_event_unclaimed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, worker_session: None
) -> None:
    """The claim must not outlive the work it guards.

    If a transient failure left the claim committed, the retry would find
    the event already claimed and skip it -- turning a duplicate, which
    is harmless, into a lost event, which is not.
    """

    def boom(db: Session, envelope: dict[str, Any]) -> None:
        raise RuntimeError("database is briefly unreachable")

    monkeypatch.setattr(consumer_module, "dispatch", boom)

    _run([_message(ENVELOPE, offset=11)])

    remaining = db_session.execute(
        select(ProcessedEvent).where(ProcessedEvent.event_id == "evt-loop-1")
    ).scalar_one_or_none()
    assert remaining is None

"""Tests for publish()'s delivery check, with a fake Producer.

No broker, and none needed: what is under test is our own decision about
when a message counts as delivered, not Kafka's behaviour. That decision had
a real bug in it -- see test_a_delivery_error_fails_even_when_the_queue_is_empty
-- which cost a live debugging session to find, so it is pinned here.
"""

import json
from typing import Any, Callable

import pytest
from confluent_kafka import KafkaException

from app.kafka import producer


class _FakeProducer:
    """Records what it was given and fakes a broker's response.

    outstanding is what flush() reports still queued; delivery_error is what
    librdkafka would hand the callback. The two are independent on purpose,
    because in real life they are: a message the client abandons is removed
    from the queue, so flush() can report zero outstanding for a message
    that never arrived.
    """

    def __init__(self, outstanding: int = 0, delivery_error: str | None = None) -> None:
        self.outstanding = outstanding
        self.delivery_error = delivery_error
        self.produced: list[dict[str, Any]] = []
        self._callbacks: list[Callable[[object, object], None]] = []

    def produce(
        self,
        topic: str,
        key: str,
        value: bytes,
        on_delivery: Callable[[object, object], None],
    ) -> None:
        self.produced.append({"topic": topic, "key": key, "value": value})
        self._callbacks.append(on_delivery)

    def flush(self, timeout: float) -> int:
        """Fire the delivery callbacks, as librdkafka does, then report."""
        for callback in self._callbacks:
            callback(self.delivery_error, None)
        return self.outstanding


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeProducer:
    """Install a fake in place of the real, cached Producer."""
    instance = _FakeProducer()
    monkeypatch.setattr(producer, "get_producer", lambda: instance)
    return instance


def test_a_delivered_message_does_not_raise(fake: _FakeProducer) -> None:
    producer.publish("app.appointments", "17", {"event_id": "abc"})

    assert len(fake.produced) == 1


def test_the_message_is_json_encoded_bytes(fake: _FakeProducer) -> None:
    """Kafka carries bytes. The consumer decodes JSON, so anything else
    here breaks it at the far end rather than at this line."""
    producer.publish("app.appointments", "17", {"event_id": "abc", "version": 1})

    sent = fake.produced[0]
    assert sent["topic"] == "app.appointments"
    assert sent["key"] == "17"
    assert json.loads(sent["value"].decode()) == {"event_id": "abc", "version": 1}


def test_an_outstanding_message_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """flush() timing out with messages still queued means the broker never
    confirmed, so the caller must not mark the event published."""
    monkeypatch.setattr(producer, "get_producer", lambda: _FakeProducer(outstanding=1))

    with pytest.raises(KafkaException):
        producer.publish("app.appointments", "17", {"event_id": "abc"})


def test_a_delivery_error_fails_even_when_the_queue_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this file exists for.

    A message librdkafka gives up on is *removed* from its queue, so
    flush() reports zero outstanding for a message that never arrived.
    Trusting flush() alone would stamp published_at on a lost event -- the
    exact failure the outbox is built to prevent, reintroduced one layer
    up. Only the delivery callback tells the two apart.
    """
    monkeypatch.setattr(
        producer,
        "get_producer",
        lambda: _FakeProducer(outstanding=0, delivery_error="Message timed out"),
    )

    with pytest.raises(KafkaException, match="not acknowledged"):
        producer.publish("app.appointments", "17", {"event_id": "abc"})

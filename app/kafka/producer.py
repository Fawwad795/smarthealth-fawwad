"""The Kafka producer: one per process, and delivery actually confirmed."""

import json
from functools import lru_cache

from confluent_kafka import KafkaException, Producer

from app.core.config import settings

# How long to wait for the broker to acknowledge one message.
_FLUSH_TIMEOUT_SECONDS = 10.0


@lru_cache(maxsize=1)
def get_producer() -> Producer:
    """One Producer per process, built on first use.

    A Producer owns a background thread and its own connections, so one
    per message would be slow and would leak sockets. Built lazily rather
    than at module level so importing this module does not depend on Kafka
    being reachable -- otherwise the API would fail to start whenever the
    broker was down, for the sake of a background concern.
    """
    return Producer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            # Wait for every in-sync replica. There is only one broker
            # here, so this is mostly a statement of intent: a message the
            # relay is about to mark published must really be durable.
            "acks": "all",
            "enable.idempotence": True,
            # Let librdkafka give up *before* our flush window closes, so a
            # message we report as failed is not still sitting in its queue
            # waiting to be delivered later. Without this the client retries
            # for five minutes by default, and every failed relay run leaves
            # another copy behind -- observed live as five deliveries of one
            # outbox row after a broker restart. The outbox is the retry
            # mechanism; the client must not be a second one.
            "message.timeout.ms": 8000,
        }
    )


def publish(topic: str, key: str, value: dict[str, object]) -> None:
    """Produce one message and block until the broker confirms it.

    produce() is asynchronous -- it queues the message on a background
    thread and returns immediately, so on its own it proves nothing. Even
    flush() is not enough on its own: a message librdkafka has given up on
    is *removed* from the queue, so flush() reports nothing outstanding for
    a message that never arrived. Only the delivery callback distinguishes
    "sent" from "abandoned".

    Both are checked, and either one failing means the caller must not
    record this event as published.

    One flush per message, deliberately. Batching would be faster, but a
    partial failure would leave us unable to say which messages made it,
    and marking the wrong row published loses an event permanently.
    Throughput is not this system's problem; a silently dropped event is.
    """
    failures: list[str] = []

    def on_delivery(err: object, msg: object) -> None:
        """Called by librdkafka once the message's fate is settled."""
        if err is not None:
            failures.append(str(err))

    producer = get_producer()
    producer.produce(
        topic=topic,
        key=key,
        value=json.dumps(value).encode(),
        on_delivery=on_delivery,
    )

    outstanding = producer.flush(_FLUSH_TIMEOUT_SECONDS)
    if outstanding or failures:
        raise KafkaException(
            f"message for {topic} not acknowledged "
            f"(outstanding={outstanding}, errors={failures})"
        )

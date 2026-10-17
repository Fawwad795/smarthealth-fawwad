"""The Kafka consumer: the process that turns published events into numbers.

The fourth kind of process here, and the only one nobody triggers. The API
answers a request, a Celery task runs because something enqueued it, a
Workflow advances because Temporal drove it. This just runs, asking Kafka
for whatever has arrived since last time.

Its correctness question is entirely about *when the offset moves*. Kafka
does not remove a message when it is read; it records a per-partition
bookmark for the consumer group. Commit before processing and a crash
skips the event permanently; commit after, and a crash reprocesses it.
The second is the right trade, and it is why processed_events exists.
"""

import json
import logging
import signal
import threading
import time
from typing import Any

from confluent_kafka import Consumer, Message, TopicPartition
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import configure_logging, set_correlation_id
from app.core.metrics import (
    CONSUMER_METRICS_PORT,
    events_consumed,
    events_failed,
    start_metrics_server,
)
from app.db.session import session_scope
from app.events.envelope import EventType, all_topics
from app.kafka.dedupe import claim_event
from app.kafka.errors import PermanentEventError
from app.kafka.handlers import HANDLERS
from app.models import FailedJob

logger = logging.getLogger(__name__)

# How long poll() waits for a message before returning None. Short enough
# that a SIGTERM is noticed promptly, long enough not to spin.
POLL_TIMEOUT_SECONDS = 1.0

# Pause after a transient failure, so a database outage does not become a
# hot loop re-reading the same message thousands of times a second.
RETRY_BACKOFF_SECONDS = 2.0

# Fields every envelope must carry. Absent means the producer is broken or
# the message is not ours -- either way, retrying will not help.
_REQUIRED_FIELDS = ("event_id", "event_type", "data")


def consumer_config() -> dict[str, Any]:
    """The Consumer settings.

    Separate from build_consumer() so a test can assert on these values
    without constructing a real client. Three of them are correctness
    decisions rather than tuning, and each would fail silently if it were
    quietly changed back to a default.
    """
    return {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "group.id": settings.kafka_consumer_group,
        # The offset moves when *we* say so, in _handle_one, after the
        # work has committed. The default commits on a five-second
        # timer regardless of whether processing succeeded, which is
        # precisely the "skipped forever" failure.
        "enable.auto.commit": False,
        # A group with no committed offset starts at the beginning of
        # the topic, not at "now". With `latest`, a consumer started
        # after a booking would ignore every event already published
        # and look perfectly healthy while the analytics stayed at 0.
        "auto.offset.reset": "earliest",
        # Topics are created by the first produce to them, which can
        # happen long after this consumer subscribed. librdkafka
        # defaults to refreshing topic metadata every five minutes,
        # so a topic created after startup stays invisible for up to
        # that long -- observed live: app.visits did not exist when
        # the consumer started, and a visit.completed event published
        # 45 seconds later was not seen until the process restarted.
        # Ten seconds keeps the blind window demo-sized. The real fix
        # is creating topics up front, which is a deployment concern
        # this project does not have.
        "topic.metadata.refresh.interval.ms": 10000,
    }


def build_consumer() -> Consumer:
    """The configured Consumer. Split out so tests never need a broker."""
    return Consumer(consumer_config())


def parse_message(msg: Message) -> dict[str, Any]:
    """Decode and validate one message, or raise PermanentEventError.

    Every failure here is permanent by nature: malformed JSON does not
    become well-formed on a retry.
    """
    try:
        envelope = json.loads(msg.value())
    except (TypeError, ValueError) as exc:
        raise PermanentEventError(f"payload is not valid JSON: {exc}") from exc

    if not isinstance(envelope, dict):
        raise PermanentEventError("payload is not a JSON object")

    missing = [field for field in _REQUIRED_FIELDS if field not in envelope]
    if missing:
        raise PermanentEventError(f"envelope is missing fields: {missing}")

    try:
        EventType(envelope["event_type"])
    except ValueError as exc:
        raise PermanentEventError(
            f"unknown event type: {envelope['event_type']}"
        ) from exc

    return envelope


def dispatch(db: Session, envelope: dict[str, Any]) -> None:
    """Route one event to its handler.

    An event type with no handler is not an error. Topics are per
    aggregate, so this consumer legitimately receives events it has no
    interest in -- refusing them would dead-letter perfectly good
    messages for the crime of not being about analytics.
    """
    handler = HANDLERS.get(EventType(envelope["event_type"]))
    if handler is None:
        logger.debug("no handler for event_type=%s", envelope["event_type"])
        return
    handler(db, envelope)


def _dead_letter(msg: Message, exc: Exception) -> None:
    """Record a message that can never succeed, so stepping over it is visible.

    Coordinates only -- topic, partition, offset -- never the body. Our
    own events carry ids, but a message that failed to parse is by
    definition of unknown shape, and failed_jobs must not become the
    place PHI arrives by accident (rule 6.6). The coordinates are enough
    to go and read it in Kafka UI.

    Uses its own session, deliberately: the caller's may have just been
    rolled back by the failure being recorded.
    """
    logger.error(
        "dead-lettering message topic=%s partition=%s offset=%s reason=%s",
        msg.topic(),
        msg.partition(),
        msg.offset(),
        exc,
    )
    with session_scope() as db:
        db.add(
            FailedJob(
                job_type="app.kafka.consumer",
                payload={
                    "topic": msg.topic(),
                    "partition": msg.partition(),
                    "offset": msg.offset(),
                },
                error=str(exc),
                # One attempt, and no retry: re-attempting something that
                # can never succeed only blocks the partition behind it.
                attempts=1,
            )
        )
        db.commit()


def _rewind(consumer: Consumer, msg: Message) -> None:
    """Put the read position back so this message is delivered again.

    Not committing is *not* enough on its own, which is the subtlety
    worth knowing: poll() advances the client's own position whether or
    not the offset was committed, so the next call would return the
    following message and this one would only be retried after a restart
    or a rebalance. seek() is what actually makes "we will try again"
    true within a running process.
    """
    consumer.seek(TopicPartition(msg.topic(), msg.partition(), msg.offset()))


def _handle_one(consumer: Consumer, msg: Message) -> None:
    """Process one message and decide whether its offset may move."""
    try:
        envelope = parse_message(msg)
        # The id minted by the request that caused this event, carried all
        # the way from the API through the outbox. Setting it here is what
        # makes one booking greppable across API, worker and consumer.
        set_correlation_id(envelope.get("correlation_id"))

        with session_scope() as db:
            if claim_event(db, envelope["event_id"], settings.kafka_consumer_group):
                try:
                    dispatch(db, envelope)
                    # One commit for the claim and whatever the handler
                    # changed, so the two can never disagree about
                    # whether this event was handled.
                    db.commit()
                except Exception:
                    # Undo the claim explicitly rather than leaving it to
                    # the session being closed on the way out. Closing
                    # does roll back, but a claim outliving the work it
                    # guards would turn a harmless duplicate into a
                    # permanently lost event -- the worse of the two
                    # failures -- and a guarantee that important should
                    # be stated here rather than inherited from a
                    # context manager's cleanup.
                    db.rollback()
                    raise
                logger.info(
                    "event processed event_id=%s type=%s",
                    envelope["event_id"],
                    envelope["event_type"],
                )
                events_consumed.labels(envelope["event_type"], "processed").inc()
            else:
                db.rollback()
                # A skip, not a failure: this event was handled earlier,
                # so its offset must still move past it below.
                logger.info(
                    "duplicate event skipped event_id=%s type=%s",
                    envelope["event_id"],
                    envelope["event_type"],
                )
                # A duplicate is counted, not ignored. "How many replays are
                # we absorbing" is a real question, and a silent skip makes
                # a producer stuck in a retry loop invisible.
                events_consumed.labels(envelope["event_type"], "duplicate").inc()

    except PermanentEventError as exc:
        events_failed.labels("permanent").inc()
        _dead_letter(msg, exc)
        # Falls through to the commit below on purpose: the message is
        # recorded, and the stream must move past it.
    except Exception:
        events_failed.labels("transient").inc()
        logger.exception("event handling failed; offset left for redelivery")
        _rewind(consumer, msg)
        time.sleep(RETRY_BACKOFF_SECONDS)
        return

    consumer.commit(msg)


def consume_forever(consumer: Consumer, stop: threading.Event) -> None:
    """The loop. Runs until `stop` is set -- by SIGTERM, or by a test.

    The stop Event is not ceremony: without an injectable way out, this
    function could only ever be tested by killing the process running it.
    """
    while not stop.is_set():
        msg = consumer.poll(POLL_TIMEOUT_SECONDS)
        if msg is None:
            continue
        if msg.error():
            # Broker-level notices (partition EOF, a rebalance in
            # progress) are not our messages and carry no payload.
            logger.warning("kafka reported: %s", msg.error())
            continue
        _handle_one(consumer, msg)


def main() -> None:
    """Entry point for `python -m app.kafka.consumer`."""
    configure_logging()
    start_metrics_server(CONSUMER_METRICS_PORT)
    stop = threading.Event()

    def request_stop(signum: int, frame: object) -> None:
        """Ask the loop to finish the message it is on, then exit."""
        logger.info("stop requested signal=%s", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    consumer = build_consumer()
    consumer.subscribe(all_topics())
    logger.info("consumer started group=%s", settings.kafka_consumer_group)

    try:
        consume_forever(consumer, stop)
    finally:
        # Leaves the group deliberately rather than timing out, so a
        # restart reassigns partitions in seconds instead of waiting for
        # the session timeout to expire.
        consumer.close()
        logger.info("consumer stopped")


if __name__ == "__main__":
    main()

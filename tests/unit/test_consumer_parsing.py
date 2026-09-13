"""parse_message: what the consumer accepts, and what it refuses outright.

Every rejection here raises PermanentEventError rather than a plain
exception, and that distinction is the whole point -- it is what decides
whether the offset moves past the message or the message is retried
forever. A parse failure that were treated as transient would block its
partition permanently.
"""

import json

import pytest

from app.kafka.consumer import (
    PermanentEventError,
    consumer_config,
    parse_message,
)
from tests.kafka_fakes import FakeMessage

VALID = {
    "event_id": "evt-1",
    "event_type": "visit.completed",
    "version": 1,
    "occurred_at": "2026-09-09T10:00:00+00:00",
    "correlation_id": "req-1",
    "data": {"visit_id": 5},
}


def _message(payload: object) -> FakeMessage:
    """A message carrying `payload` encoded exactly as the producer encodes it."""
    return FakeMessage(json.dumps(payload).encode())


def test_a_well_formed_envelope_is_returned_as_a_dict() -> None:
    """The positive case, and not filler: five tests that all assert
    rejection would pass just as happily against a parser that rejects
    everything.
    """
    assert parse_message(_message(VALID)) == VALID


def test_a_payload_that_is_not_json_is_permanent() -> None:
    """Malformed bytes do not become well-formed on a retry."""
    with pytest.raises(PermanentEventError, match="not valid JSON"):
        parse_message(FakeMessage(b"{not json at all"))


def test_a_json_array_is_rejected() -> None:
    """Valid JSON, wrong shape. Without this check the field loop below
    would raise TypeError instead -- an unhandled error, which the loop
    would treat as transient and retry forever.
    """
    with pytest.raises(PermanentEventError, match="not a JSON object"):
        parse_message(_message([1, 2, 3]))


@pytest.mark.parametrize("missing", ["event_id", "event_type", "data"])
def test_an_envelope_missing_a_required_field_is_rejected(missing: str) -> None:
    """event_id is the idempotency key, event_type chooses the handler and
    data carries the ids. A message without any one of them cannot be
    processed by anything downstream.
    """
    payload = {key: value for key, value in VALID.items() if key != missing}
    with pytest.raises(PermanentEventError, match=missing):
        parse_message(_message(payload))


def test_an_unknown_event_type_is_rejected() -> None:
    """A type outside EventType means a producer this consumer does not
    understand -- a deploy-ordering problem, not a transient one.
    """
    payload = VALID | {"event_type": "appointment.teleported"}
    with pytest.raises(PermanentEventError, match="unknown event type"):
        parse_message(_message(payload))


def test_the_correctness_critical_consumer_settings_are_pinned() -> None:
    """Three of the Consumer's settings are decisions, not tuning.

    Each fails silently if reverted to its default: auto-commit would
    move the offset on a timer whether or not processing succeeded,
    `latest` would make a fresh consumer ignore every event already
    published, and the five-minute metadata default hides a topic created
    after subscribe -- all three look like a healthy consumer producing
    no numbers.
    """
    config = consumer_config()

    assert config["enable.auto.commit"] is False
    assert config["auto.offset.reset"] == "earliest"
    assert config["topic.metadata.refresh.interval.ms"] == 10000

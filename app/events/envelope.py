"""The event catalogue and the envelope every published event shares.

EventType lives here rather than in app/models/enums.py because it is a
wire contract -- other processes parse these strings -- not a database
enum. The events table stores it as plain text for the same reason: a
Postgres enum would need a migration before a new event type could even be
written down.
"""

from enum import Enum

from app.core.config import settings
from app.models import OutboxEvent

# Bumped only when the shape of `data` changes incompatibly. Stored on the
# outbox row rather than stamped at publish time, because it describes the
# payload as it was written -- a row queued yesterday must not be relabelled
# with today's version.
EVENT_VERSION = 1


class EventType(str, Enum):
    """Every event this system publishes.

    Names are `<aggregate>.<past-tense action>`. The aggregate half is not
    decoration -- aggregate_of() reads it to pick the topic, so a new event
    type needs no extra wiring as long as it follows the pattern.

    Past tense on purpose: an event says something already happened and
    committed. It is never a request for something to happen.
    """

    APPOINTMENT_BOOKED = "appointment.booked"
    APPOINTMENT_CONFIRMED = "appointment.confirmed"
    APPOINTMENT_CANCELLED = "appointment.cancelled"
    VISIT_COMPLETED = "visit.completed"
    SERVICE_PUBLISHED = "service.published"
    BILLING_UPDATED = "billing.updated"


# Plural topic names, keyed by the aggregate half of the event type.
_TOPIC_SUFFIX = {
    "appointment": "appointments",
    "visit": "visits",
    "service": "services",
    "billing": "billings",
}


def aggregate_of(event_type: str) -> str:
    """The aggregate an event belongs to: "appointment.booked" -> "appointment"."""
    return event_type.split(".", 1)[0]


def topic_for(event_type: str) -> str:
    """The Kafka topic an event is published to.

    One topic per aggregate, not per event type. All of an appointment's
    events therefore share a topic and -- because the message key is the
    appointment id -- a partition, so a consumer always sees `booked`
    before `confirmed`. Splitting them across topics would give no
    ordering guarantee between the two at all.
    """
    return f"{settings.kafka_topic_prefix}.{_TOPIC_SUFFIX[aggregate_of(event_type)]}"


def build_envelope(event: "OutboxEvent") -> dict[str, object]:
    """The wire format, built from a queued row.

    occurred_at comes from created_at rather than a column of its own:
    they are the same moment, and created_at is filled by Postgres, so
    every event across all four processes is stamped by one clock.
    """
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "version": event.version,
        "occurred_at": event.created_at.isoformat(),
        "correlation_id": event.correlation_id,
        "data": event.data,
    }


def all_topics() -> list[str]:
    """Every topic this system publishes to -- the consumer's subscription.

    Derived from the same map topic_for() uses, so an added event type
    needs no second edit here. A hand-written list is exactly the "two
    sources that can disagree" problem the aggregate-from-name rule
    already avoids.
    """
    return [
        f"{settings.kafka_topic_prefix}.{suffix}" for suffix in _TOPIC_SUFFIX.values()
    ]

"""Tests for the event catalogue: names, values and topic routing.

These need no database. They exist because the event names are a wire
contract -- other processes parse these exact strings -- and a mistyped one
is invisible to ruff, black and mypy alike. Three of the checks below would
have caught a real transcription slip on the day this file was written, in
which two members had their values crossed over and a third was misspelled
into an aggregate that no topic exists for.
"""

import pytest

from app.core.config import settings
from app.events.envelope import (
    EVENT_VERSION,
    _TOPIC_SUFFIX,
    EventType,
    aggregate_of,
    topic_for,
)


def test_every_event_name_matches_its_value() -> None:
    """APPOINTMENT_CONFIRMED must be "appointment.confirmed" and nothing
    else. The name and the value say the same thing twice, so they can
    disagree -- and a member whose value belongs to a different member is
    the kind of mistake that publishes an event under the wrong type
    without anything failing.
    """
    for member in EventType:
        assert member.value == member.name.lower().replace("_", ".", 1)


def test_every_event_belongs_to_a_known_aggregate() -> None:
    """A misspelled aggregate half -- "apointment.cancelled" -- would raise
    KeyError inside topic_for at publication time, long after the event was
    accepted and committed. Caught here instead."""
    for member in EventType:
        assert aggregate_of(member.value) in _TOPIC_SUFFIX


def test_every_event_type_resolves_to_a_topic() -> None:
    """The end-to-end version of the check above: no event type may fail
    to route."""
    for member in EventType:
        assert topic_for(member.value).startswith(f"{settings.kafka_topic_prefix}.")


def test_all_of_an_appointments_events_share_one_topic() -> None:
    """The reason topics are per aggregate rather than per event type: a
    consumer must see `booked` before `confirmed` for the same appointment,
    and Kafka only orders within a partition of one topic."""
    topics = {
        topic_for(EventType.APPOINTMENT_BOOKED.value),
        topic_for(EventType.APPOINTMENT_CONFIRMED.value),
        topic_for(EventType.APPOINTMENT_CANCELLED.value),
    }

    assert len(topics) == 1


def test_event_values_are_unique() -> None:
    """Two members sharing a value is legal Python -- the second becomes an
    alias of the first and silently disappears from iteration."""
    values = [member.value for member in EventType]

    assert len(values) == len(set(values))


def test_aggregate_of_takes_only_the_first_segment() -> None:
    assert aggregate_of("appointment.booked") == "appointment"


def test_event_version_is_a_positive_integer() -> None:
    assert isinstance(EVENT_VERSION, int)
    assert EVENT_VERSION >= 1


@pytest.mark.parametrize("bad", ["", "nodot", "unknown.thing"])
def test_topic_for_rejects_anything_not_in_the_catalogue(bad: str) -> None:
    """Routing fails loudly rather than inventing a topic name. A silently
    invented topic would accept messages nothing is subscribed to."""
    with pytest.raises(KeyError):
        topic_for(bad)

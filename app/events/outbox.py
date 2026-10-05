"""Recording an event into the outbox.

One function, and the important thing about it is what it does *not* do.
"""

import uuid

from sqlalchemy.orm import Session

from app.core.logging import get_correlation_id
from app.events.envelope import EVENT_VERSION, EventType
from app.models import OutboxEvent


def record_event(
    db: Session,
    event_type: EventType,
    aggregate_id: int,
    data: dict[str, int],
) -> OutboxEvent:
    """Queue an event, inside the caller's own transaction.

    Deliberately does not commit. The caller is mid-transaction doing the
    business change this event describes, and the event has to land in
    *that* transaction -- committing here would split them apart again and
    reintroduce exactly the gap the outbox exists to close. If the caller
    rolls back, this row rolls back with it and the event correctly never
    happened.

    correlation_id is read from the ambient context rather than passed in,
    so an event carries the id of the request that caused it without every
    caller having to remember to thread it through.

    `data` is typed dict[str, int] on purpose: an event carries ids, never
    names, contacts or free text (rule 6.6). The type is the reminder.
    """
    event = OutboxEvent(
        event_id=str(uuid.uuid4()),
        event_type=event_type.value,
        version=EVENT_VERSION,
        aggregate_id=aggregate_id,
        correlation_id=get_correlation_id(),
        data=data,
    )
    db.add(event)
    return event

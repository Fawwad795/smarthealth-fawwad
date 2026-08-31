"""The status vocabularies of the domain.

This module imports nothing from the rest of the application. Models,
services, schemas, Temporal activities and tests all need these values, so
keeping it a leaf module means anything may import it and it can never take
part in a circular import.

Member names are identical to their values and everything is uppercase.
These strings are written into database rows and into event payloads, so
humans read them in psql output and in logs; renaming one later is a data
migration plus a constraint migration, not a code change.
"""

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


class UserRole(StrEnum):
    """The role of a user in the system.

    One role per user. Someone genuinely needing two needs two accounts —
    see the decisions table in docs/design.md.
    """

    PATIENT = "PATIENT"
    PROVIDER = "PROVIDER"
    FRONT_DESK = "FRONT_DESK"
    ADMIN = "ADMIN"


class ServiceStatus(StrEnum):
    """Lifecycle of a service, moved by the Week 2 publish workflow.

    DRAFT -> PUBLISHING -> PUBLISHED -> INACTIVE
             PUBLISHING -> PUBLISH_FAILED -> PUBLISHING (retry)

    PUBLISHING and PUBLISH_FAILED exist so the row can say "a workflow is
    running right now" and "the last attempt failed" — states a boolean
    is_published cannot hold, and the reason GET /services/{id}/publish-status
    is answerable at all.
    """

    DRAFT = "DRAFT"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    INACTIVE = "INACTIVE"


class SlotStatus(StrEnum):
    """Whether a slot of provider time can be booked.

    This is the column the atomic reservation guards on:

        UPDATE slots SET status = 'RESERVED', updated_at = now()
        WHERE id = :slot_id AND status = 'AVAILABLE'
        RETURNING id;

    RESERVED and BOOKED are deliberately separate. Compensation after a
    billing failure releases a slot back to AVAILABLE, and it can only know
    that is safe if the status says the booking never completed.
    """

    AVAILABLE = "AVAILABLE"  # generated and bookable
    RESERVED = "RESERVED"  # held by an in-flight saga, may be released
    BOOKED = "BOOKED"  # a confirmed appointment owns this slot
    BLOCKED = "BLOCKED"  # provider time off, never bookable


class AppointmentStatus(StrEnum):
    """Lifecycle of a booking, driven by the Week 2 scheduling saga.

        REQUESTED --reserve--> SLOT_RESERVED --billing--> CONFIRMED --(visit)--> COMPLETED
            |                       |
         (ineligible)         (billing fails)
            v                       v
        REJECTED         compensate(release slot) -> CANCELLED
                                                        ^
                                patient cancel / reschedule

    SLOT_RESERVED is its own state, not folded into CONFIRMED, because the
    saga's compensation depends on knowing whether a slot was ever held for
    this appointment -- see AppointmentStatusHistory.
    """

    REQUESTED = "REQUESTED"
    SLOT_RESERVED = "SLOT_RESERVED"
    CONFIRMED = "CONFIRMED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class BillingStatus(StrEnum):
    """Lifecycle of a simulated billing pre-check for one appointment.

    Real payment/insurance integration is out of scope (CLAUDE.md #4) --
    this exists so the Week 2 scheduling saga (task 2.9) has a genuine
    failure to compensate against. PENDING is the vocabulary's "not yet
    checked" state; in practice precheck() decides CHECKED or FAILED in
    one synchronous write, so PENDING is never actually persisted by this
    simulation. REFUNDED is reserved for a future cancellation path.
    """

    PENDING = "PENDING"
    CHECKED = "CHECKED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class ContentSourceType(StrEnum):
    """What kind of row a content_chunks entry was generated from.

    A single member for now -- services are the only thing this project
    chunks. Kept as an enum (source_type + source_id) rather than a bare
    service_id column because the brief's data model treats content_chunks
    as generic: whatever gets chunked later says so through this column,
    not through a schema change.
    """

    SERVICE = "SERVICE"


def enum_column(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Build the column type for an enum: VARCHAR + CHECK, never a native type.

    Two settings that must never be forgotten, which is the whole reason this
    is a function rather than four arguments repeated at every call site:

    native_enum=False  - stores the value as VARCHAR guarded by a check
        constraint instead of a Postgres CREATE TYPE. Status values get added
        and renamed as the workflows are built, and a native enum has no clean
        reversal: Postgres has no DROP VALUE, so downgrading a value addition
        means recreating the type and re-casting the column.

    create_constraint=True - NOT the SQLAlchemy default. Without it the column
        is an unconstrained VARCHAR that enforces nothing in the database, and
        a seed script or psql session could write any string at all.

    `name` becomes the constraint's suffix under the ck_ naming convention in
    app/db/base.py, so enum_column(SlotStatus, "status") on the slots table
    produces the constraint ck_slots_status.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
    )

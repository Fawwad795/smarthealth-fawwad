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

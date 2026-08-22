"""The status vocabularies. No database, no infrastructure of any kind.

These values are written into database rows and into event payloads, so
renaming one later is a data migration plus a constraint migration rather
than a code change. This file is what fails loudly if someone edits a member
without thinking about the rows already holding the old string.

It also pins the two properties the rest of the system relies on: member
names equal their values (SQLAlchemy persists .name while a str-based enum
serialises .value, so a divergence would mean the database holds one string
and the API returns another), and StrEnum formatting, so a log line renders
AVAILABLE rather than SlotStatus.AVAILABLE.
"""

from app.models.enums import ServiceStatus, SlotStatus, UserRole


def test_slot_status_members() -> None:
    """The four states the Week 2 saga moves a slot between.

    RESERVED and BOOKED must stay distinct: compensation after a billing
    failure releases a slot back to AVAILABLE, and it can only know that is
    safe if the status says the booking never completed.
    """
    assert [m.value for m in SlotStatus] == [
        "AVAILABLE",
        "RESERVED",
        "BOOKED",
        "BLOCKED",
    ]


def test_service_status_members() -> None:
    """The states the Week 2 publish workflow moves through.

    PUBLISHING and PUBLISH_FAILED are what let the row say "a workflow is
    running right now" and "the last attempt failed" -- states an
    is_published boolean cannot hold, and the reason
    GET /services/{id}/publish-status is answerable.
    """
    assert [m.value for m in ServiceStatus] == [
        "DRAFT",
        "PUBLISHING",
        "PUBLISHED",
        "PUBLISH_FAILED",
        "INACTIVE",
    ]


def test_user_role_members() -> None:
    assert [m.value for m in UserRole] == [
        "PATIENT",
        "PROVIDER",
        "FRONT_DESK",
        "ADMIN",
    ]


def test_member_names_equal_their_values() -> None:
    """SQLAlchemy persists .name; Pydantic serialises .value.

    If the two ever diverge, the database holds one string while the API
    returns another, and a hand-written query in docs/runbook.md silently
    matches nothing.
    """
    for enum_cls in (SlotStatus, ServiceStatus, UserRole):
        for member in enum_cls:
            assert member.name == member.value, enum_cls.__name__


def test_formatting_yields_the_bare_value() -> None:
    """StrEnum rather than (str, Enum).

    With the classic (str, Enum) form an f-string renders
    "SlotStatus.AVAILABLE". Half the log lines would then carry a different
    vocabulary from the database rows, and grepping for a status would match
    only some of them.
    """
    assert f"{SlotStatus.AVAILABLE}" == "AVAILABLE"
    assert SlotStatus.AVAILABLE == "AVAILABLE"

"""The schema's guarantees, against a database built by the migrations.

These do not test that Postgres implements UNIQUE correctly. They test that
the migration created the constraint I think it did, on the columns I think it
did -- a claim that a later migration, a merge, or a model edited without a
revision can silently break, leaving a schema that still works but no longer
protects anything.

Every test names the expected constraint in `match=`. That is only possible
because the names come from the convention in app/db/base.py rather than being
invented by Postgres. Without it, any IntegrityError would satisfy the test,
including one caused by a typo in the test data.

One illegal act per test: after an IntegrityError the transaction is aborted
and Postgres rejects every subsequent statement, so a second violation in the
same test would prove nothing.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Department, Patient, Provider, Service, Slot, User
from app.models.enums import UserRole

# Fixed, never datetime.now(). A test whose input changes on every run can
# fail for reasons unrelated to the code, and the reflex that produces --
# "just run it again" -- is how a real failure eventually gets re-run away.
# 04:00Z is 09:00 in Asia/Karachi.
BASE = datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc)
QUARTER = timedelta(minutes=15)


def test_email_uniqueness_ignores_capitalisation(db_session: Session) -> None:
    """Two accounts for one person, differing only in case, must be impossible.

    Postgres compares text byte-for-byte, so a plain unique constraint on the
    column would allow both. This is enforced by a unique index on
    lower(email) instead.
    """
    db_session.add(
        User(email="Fawwad@example.com", password_hash="x", role=UserRole.PATIENT)
    )
    db_session.flush()

    db_session.add(
        User(email="fawwad@example.com", password_hash="x", role=UserRole.PATIENT)
    )
    with pytest.raises(IntegrityError, match="uq_users_email_lower"):
        db_session.flush()


def test_a_user_cannot_have_two_patient_profiles(
    db_session: Session, patient_user: User
) -> None:
    """The UNIQUE on patients.user_id is what makes the one-to-one real.

    Without it, "the patient row for this user" -- the basis of every
    PHI-scoped query -- would no longer identify a single row, and the
    authorization boundary itself would become ambiguous.
    """
    db_session.add(Patient(user_id=patient_user.id, dob=date(1995, 3, 2)))
    db_session.flush()

    db_session.add(Patient(user_id=patient_user.id, dob=date(1990, 1, 1)))
    with pytest.raises(IntegrityError, match="uq_patients_user_id"):
        db_session.flush()


def test_overlapping_slots_for_one_provider_are_rejected(
    db_session: Session, provider: Provider
) -> None:
    """The guarantee no unique constraint can express.

    09:05-09:20 has a different start_time from 09:00-09:15, so
    UNIQUE (provider_id, start_time) permits it. Only the exclusion constraint
    catches it -- and it is evaluated as part of the insert, so unlike a
    look-before-you-insert check in the generator there is no window in which
    two concurrent runs could both pass.
    """
    db_session.add(
        Slot(provider_id=provider.id, start_time=BASE, end_time=BASE + QUARTER)
    )
    db_session.flush()

    db_session.add(
        Slot(
            provider_id=provider.id,
            start_time=BASE + timedelta(minutes=5),
            end_time=BASE + timedelta(minutes=20),
        )
    )
    with pytest.raises(IntegrityError, match="ex_slots_no_overlap"):
        db_session.flush()


def test_adjacent_slots_are_accepted(db_session: Session, provider: Provider) -> None:
    """The positive case, and not filler.

    A constraint that is too strict is as broken as one that is missing, and
    here it would be fatal: on a 15-minute grid every slot's end is the next
    one's start. tstzrange is half-open -- [start, end) -- so touching
    boundaries do not overlap. Seven tests that all assert rejection would
    pass just as happily against a table that rejects everything.
    """
    for i in range(4):
        db_session.add(
            Slot(
                provider_id=provider.id,
                start_time=BASE + i * QUARTER,
                end_time=BASE + (i + 1) * QUARTER,
            )
        )
    db_session.flush()

    count = db_session.scalar(
        text("SELECT count(*) FROM slots WHERE provider_id = :p"),
        {"p": provider.id},
    )
    assert count == 4


def test_zero_length_slot_is_rejected(
    db_session: Session, provider: Provider
) -> None:
    """Not merely tidiness -- it closes a hole in the exclusion constraint.

    An empty range overlaps nothing in Postgres, not even an identical empty
    range, so without this check a zero-length slot would slip past
    ex_slots_no_overlap entirely however many identical rows already existed.
    The check is what guarantees every range is non-empty, and therefore what
    makes the overlap guarantee apply to every row.
    """
    db_session.add(Slot(provider_id=provider.id, start_time=BASE, end_time=BASE))
    with pytest.raises(IntegrityError, match="ck_slots_end_after_start"):
        db_session.flush()


def test_status_outside_the_enum_is_rejected(
    db_session: Session, provider: Provider
) -> None:
    """Raw SQL on purpose: the path a seed script or a psql session takes.

    Going through the ORM would only prove that Python rejected the value.
    The guarantee has to hold for writers that never run our code, which is
    why the enum is stored as VARCHAR with a check constraint rather than
    trusted to the application.
    """
    with pytest.raises(IntegrityError, match="ck_slots_status"):
        db_session.execute(
            text(
                "INSERT INTO slots (provider_id, start_time, end_time, status) "
                "VALUES (:p, :s, :e, 'OPEN')"
            ),
            {"p": provider.id, "s": BASE, "e": BASE + QUARTER},
        )


def test_department_with_services_cannot_be_deleted(
    db_session: Session, department: Department
) -> None:
    """ON DELETE RESTRICT, and why it matters beyond referential tidiness.

    A cascade here would mean that by Week 2, deleting a department removes
    its services and their appointments -- retroactively changing analytics
    that are meant to reconcile with the raw tables, and destroying the audit
    trail. Retiring a department is done by marking its services INACTIVE,
    which is why that enum member exists.

    Deleted with raw SQL rather than db_session.delete(department): the ORM's
    default cascade would first try to orphan the services by nulling their
    department_id, and the test would be exercising that instead of the
    foreign key.
    """
    db_session.add(Service(department_id=department.id, name="Echocardiogram"))
    db_session.flush()

    with pytest.raises(IntegrityError, match="fk_services_department_id_departments"):
        db_session.execute(
            text("DELETE FROM departments WHERE id = :id"), {"id": department.id}
        )

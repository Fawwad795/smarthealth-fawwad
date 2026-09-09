"""The seed script: builds a demo dataset, and is safe to run twice.

Not exercised via subprocess -- scripts/seed.py's seed() function is
imported directly and run against the test database, the same way every
other integration test in this suite calls into a service module.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models import Clinic, Department, Patient, Provider, Service, Slot, User
from scripts.seed import SEED_ACCOUNTS, SEED_PASSWORD, seed


def _count(db: Session, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def test_seed_creates_the_expected_entities(db_session: Session) -> None:
    seed(db_session)

    assert _count(db_session, Clinic) == 1
    assert _count(db_session, Department) == 3
    assert _count(db_session, Provider) == 3
    assert _count(db_session, Service) == 3
    assert _count(db_session, Patient) == 3
    assert _count(db_session, Slot) > 0


def test_seed_is_idempotent(db_session: Session) -> None:
    seed(db_session)
    models = (Clinic, Department, Provider, Service, Patient, User, Slot)
    first_counts = {model: _count(db_session, model) for model in models}

    seed(db_session)
    second_counts = {model: _count(db_session, model) for model in models}

    assert first_counts == second_counts


def test_seed_repairs_a_drifted_password(db_session: Session) -> None:
    """A seeded account whose password changed is restored by a re-run.

    Pins the real defect: get-or-create returned early on an existing
    row, so a broken seed login stayed broken however often you re-ran.
    """
    seed(db_session)
    user = db_session.execute(
        select(User).where(User.email == "patient.one@example.com")
    ).scalar_one()
    user.password_hash = hash_password("not-the-seed-password")
    db_session.flush()

    seed(db_session)

    assert verify_password(SEED_PASSWORD, user.password_hash)


def test_advertised_accounts_match_what_seed_creates(db_session: Session) -> None:
    """SEED_ACCOUNTS must be exactly the accounts seed() inserts.

    Without this the printed list is decoration: adding a patient and
    forgetting the list would advertise nothing about a working login,
    and removing one would advertise a login that does not exist.
    """
    seed(db_session)

    emails = set(db_session.execute(select(User.email)).scalars().all())

    assert set(SEED_ACCOUNTS) == emails


def test_seed_repairs_an_unparseable_password_hash(db_session: Session) -> None:
    """A hash passlib cannot parse is repaired, not re-raised.

    Found by running the seed against a corrupted row: the first version
    called verify_password() unguarded, so the script crashed on the one
    account it was there to fix. Logging in with such a row still 500s --
    corruption is a bug for the catch-all, not an AppError -- which makes
    re-running the seed the only recovery, so it must not crash.
    """
    seed(db_session)
    user = db_session.execute(
        select(User).where(User.email == "patient.one@example.com")
    ).scalar_one()
    user.password_hash = "$2b$12$not-a-real-bcrypt-hash"
    db_session.flush()

    seed(db_session)

    assert verify_password(SEED_PASSWORD, user.password_hash)

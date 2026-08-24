"""The seed script: builds a demo dataset, and is safe to run twice.

Not exercised via subprocess -- scripts/seed.py's seed() function is
imported directly and run against the test database, the same way every
other integration test in this suite calls into a service module.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Clinic, Department, Patient, Provider, Service, Slot, User
from scripts.seed import seed


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

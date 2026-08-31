"""Fixtures shared by every test.

The schema under test is built by running the Alembic migrations, never by
Base.metadata.create_all(). create_all() builds the schema from the models,
so a migration that disagreed with them could never fail a test -- and the
migrations are what actually run on any machine that is not mine. Building
from migrations makes the suite a continuous check that the chain is correct.
"""

from collections.abc import Generator
from datetime import date
from pathlib import Path
import pytest
import redis

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.core.config import settings
from app.models import Clinic, Department, Patient, Provider, Service, Specialty, User
from app.models.enums import UserRole
from app.core.security import create_access_token
from app.db.session import get_db
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]

# CREATE DATABASE and DROP DATABASE cannot be run from inside the database
# they operate on, so they are issued against the "postgres" maintenance
# database on the same server.
_server, _, TEST_DB_NAME = settings.test_database_url.rpartition("/")
ADMIN_URL = f"{_server}/postgres"


@pytest.fixture(scope="session")
def test_database() -> Generator[str, None, None]:
    """Create the test database, migrate it to head, drop it afterwards.

    Session-scoped: replaying the migration chain takes seconds, and doing it
    per test would make the suite unusable.

    Dropped and recreated on every run rather than reused. Alembic will not
    re-apply a revision it has already recorded, so a reused database would
    keep an old schema after a migration is edited -- and the tests would
    pass against a schema that is not the one shipped.
    """
    if settings.test_database_url == settings.database_url:
        raise RuntimeError(
            "TEST_DATABASE_URL must differ from DATABASE_URL -- these "
            "fixtures issue DROP DATABASE."
        )

    # AUTOCOMMIT because CREATE/DROP DATABASE cannot run inside a transaction.
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        # WITH (FORCE) terminates any leftover connections from an
        # interrupted previous run, which would otherwise block the drop.
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))

    alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
    # Absolute paths so the suite runs from any working directory.
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    # env.py now prefers this over settings.database_url.
    alembic_cfg.set_main_option("sqlalchemy.url", settings.test_database_url)
    command.upgrade(alembic_cfg, "head")

    yield settings.test_database_url

    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="session")
def test_redis_client() -> Generator[redis.Redis, None, None]:
    """A Redis client bound to the test DB (index 15), flushed before and
    after the session so tests never depend on -- or leave behind --
    leftover keys, and never touch the app's real DB 0.
    """
    if settings.test_redis_url == settings.redis_url:
        raise RuntimeError(
            "TEST_REDIS_URL must differ from REDIS_URL -- this fixture "
            "flushes its database."
        )
    client = redis.from_url(settings.test_redis_url, decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


@pytest.fixture(scope="session")
def engine(test_database: str) -> Generator[Engine, None, None]:
    """One engine, and therefore one connection pool, for the whole run."""
    eng = create_engine(test_database)
    yield eng
    eng.dispose()


@pytest.fixture()
def db_session(engine: Engine) -> Generator[Session, None, None]:
    """A Session whose work is discarded when the test ends.

    The fixture opens a transaction the test knows nothing about and binds the
    Session inside it. join_transaction_mode="create_savepoint" means a
    commit() in the test commits a SAVEPOINT rather than the outer
    transaction, so constraints fire and errors raise exactly as they would in
    production -- but the outer transaction is never committed, and rolling it
    back at the end leaves the database untouched.

    Isolation therefore costs a rollback rather than a DELETE per table, and
    stays correct as the schema grows.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        # Matches app/db/session.py, so objects behave in tests as they do in
        # the application.
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


# --- Domain fixtures -------------------------------------------------------
# A slot needs a provider, which needs a user, a department and a specialty,
# which needs a clinic. These are fixtures rather than a make_provider()
# helper because fixtures compose: a test names only what it needs and pytest
# builds the chain in the right order, so a test wanting only a clinic does
# not pay for a provider.
#
# They flush rather than commit, so primary keys are assigned while the work
# still lives inside the transaction db_session rolls back.


@pytest.fixture()
def clinic(db_session: Session) -> Clinic:
    c = Clinic(name="MediNova Central", timezone="Asia/Karachi")
    db_session.add(c)
    db_session.flush()
    return c


@pytest.fixture()
def specialty(db_session: Session) -> Specialty:
    s = Specialty(name="Cardiology")
    db_session.add(s)
    db_session.flush()
    return s


@pytest.fixture()
def department(db_session: Session, clinic: Clinic) -> Department:
    d = Department(clinic_id=clinic.id, name="Cardiology", order_index=1)
    db_session.add(d)
    db_session.flush()
    return d


@pytest.fixture()
def provider_user(db_session: Session) -> User:
    u = User(
        email="dr.khan@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PROVIDER,
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture()
def patient_user(db_session: Session) -> User:
    u = User(
        email="patient@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture()
def provider(
    db_session: Session,
    provider_user: User,
    department: Department,
    specialty: Specialty,
) -> Provider:
    p = Provider(
        user_id=provider_user.id,
        department_id=department.id,
        specialty_id=specialty.id,
        bio="Consultant cardiologist.",
    )
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture()
def patient(db_session: Session, patient_user: User) -> Patient:
    p = Patient(user_id=patient_user.id, dob=date(1990, 1, 1))
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture()
def service(db_session: Session, department: Department) -> Service:
    s = Service(department_id=department.id, name="Echocardiogram")
    db_session.add(s)
    db_session.flush()
    return s


# --- HTTP layer -------------------------------------------------------------
# Everything above builds rows directly with the ORM. These fixtures are for
# tests that go through the real FastAPI routes instead, which is what
# route-level coverage actually requires.


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """A TestClient that sees this test's own db_session, not a fresh
    connection to the real dev database.

    app's own get_db() opens a brand-new SessionLocal() against
    settings.database_url every time FastAPI resolves it -- that's the dev
    database, not the isolated, auto-rolled-back one db_session gives this
    test. dependency_overrides swaps what Depends(get_db) resolves to, for
    the lifetime of this fixture only.
    """

    def _get_test_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _get_test_db
    with TestClient(app) as test_client:
        yield test_client
    # Cleared even though the next test's client fixture would overwrite it
    # anyway -- a stray test that used `app` directly, without this fixture,
    # must not silently inherit someone else's database override.
    app.dependency_overrides.clear()


@pytest.fixture()
def front_desk_user(db_session: Session) -> User:
    u = User(
        email="frontdesk@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.FRONT_DESK,
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture()
def admin_user(db_session: Session) -> User:
    u = User(
        email="admin@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.ADMIN,
    )
    db_session.add(u)
    db_session.flush()
    return u


def _auth_headers(user: User) -> dict[str, str]:
    # Mints a token the same way login() does, but skips the real login
    # round-trip -- bcrypt's hash+verify is deliberately slow, and a route
    # test that isn't testing login shouldn't pay for it or depend on it
    # being correct.
    token = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def patient_auth_headers(patient_user: User) -> dict[str, str]:
    return _auth_headers(patient_user)


@pytest.fixture()
def provider_auth_headers(provider_user: User) -> dict[str, str]:
    return _auth_headers(provider_user)


@pytest.fixture()
def front_desk_auth_headers(front_desk_user: User) -> dict[str, str]:
    return _auth_headers(front_desk_user)


@pytest.fixture()
def admin_auth_headers(admin_user: User) -> dict[str, str]:
    return _auth_headers(admin_user)

"""Database engine and session handling.

The engine is created once for the whole process and holds a pool of
connections. A Session is short-lived: one per request.
"""

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    # Check a pooled connection is still alive before handing it out.
    # Without this, a connection dropped by a Postgres restart surfaces as
    # a confusing error on a random later request.
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a Session and always closes it.

    Used as `db: Session = Depends(get_db)` in routers.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yields a Session for work that is *not* a web request.

    Celery tasks, Temporal Activities and the Kafka consumer all run
    outside FastAPI, so Depends(get_db) is not available to them. They
    call this instead.

    It is a deliberately thin wrapper over SessionLocal and does not
    commit or roll back -- callers commit explicitly, exactly as they did
    before. Its real job is to be the *one* place SessionLocal is looked
    up: because that lookup happens in this module's globals at call
    time, a test can redirect every caller at once by patching
    app.db.session.SessionLocal. Importing SessionLocal directly into a
    task module copies the reference at import time and defeats that,
    which is why worker code must not do it.
    """
    with SessionLocal() as db:
        yield db

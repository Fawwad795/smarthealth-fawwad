"""Database engine and session handling.

The engine is created once for the whole process and holds a pool of
connections. A Session is short-lived: one per request.
"""

from collections.abc import Generator

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

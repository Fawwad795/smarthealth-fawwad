"""FastAPI application factory.

Day 1 scope: the app boots, and /health proves it can reach Postgres.
Routers, middleware and exception handlers are added in later tasks.
"""

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db


def create_app() -> FastAPI:
    """Build and return the app. A factory (rather than a module-level
    FastAPI() call) makes it easy to build a differently-configured app
    inside tests."""
    app = FastAPI(
        title="SmartHealth API",
        description="Healthcare operations and patient engagement platform.",
        version="0.1.0",
    )

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Liveness: is this process up? Deliberately checks nothing else,
        so it stays fast and never fails because a dependency is slow."""
        return {"status": "ok", "env": settings.app_env}

    @app.get("/health/db", tags=["health"])
    def health_db(db: Session = Depends(get_db)) -> dict[str, str]:
        """Temporary Day 1 check that the API really can talk to Postgres.
        In Week 3 this is replaced by a full /health/ready that also checks
        Redis, Kafka and Temporal."""
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "reachable"}

    return app


app = create_app()

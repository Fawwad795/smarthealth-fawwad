"""FastAPI application factory.
"""

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.error_handlers import register_exception_handlers
from app.db.session import get_db

from app.api.v1.appointments import router as appointments_router
from app.api.v1.auth import router as auth_router
from app.api.v1.departments import router as departments_router
from app.api.v1.services import router as services_router
from app.api.v1.providers import router as providers_router
from app.api.v1.provider_schedules import router as provider_schedules_router


def create_app() -> FastAPI:
    """Build and return the app. A factory (rather than a module-level
    FastAPI() call) makes it easy to build a differently-configured app
    inside tests."""
    app = FastAPI(
        title="SmartHealth API",
        description=(
            "Healthcare operations and patient engagement platform. "
            "Operations only -- no diagnoses, prescriptions or medical "
            "records.\n\n"
            "Every failure returns the same envelope: "
            '`{"error": {"code": ..., "message": ...}}`.'
        ),
        version="0.1.0",
        openapi_tags=[
            {
                "name": "auth",
                "description": "Registration, login, and the current user.",
            },
            {
                "name": "departments",
                "description": "Clinic departments. Admin writes, staff read.",
            },
            {
                "name": "services",
                "description": (
                    "The service catalogue. Everything is created DRAFT; "
                    "/services/search is the one public route."
                ),
            },
            {"name": "providers", "description": "Clinician profiles."},
            {
                "name": "provider-schedules",
                "description": (
                    "Weekly templates, and generating bookable slots from them."
                ),
            },
        ],
    )

    register_exception_handlers(app)

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(departments_router, prefix="/api/v1")
    app.include_router(services_router, prefix="/api/v1")
    app.include_router(providers_router, prefix="/api/v1")
    app.include_router(provider_schedules_router, prefix="/api/v1")
    app.include_router(appointments_router, prefix="/api/v1")

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

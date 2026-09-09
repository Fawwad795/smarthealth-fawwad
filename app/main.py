"""FastAPI application factory."""

import time
from collections.abc import Awaitable, Callable

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.error_handlers import register_exception_handlers
from app.core.health import run_checks
from app.core.redis import get_redis
from app.core.logging import (
    CORRELATION_ID_HEADER,
    configure_logging,
    set_correlation_id,
)
from app.core.metrics import (
    METRICS_PATH,
    endpoint_label,
    metrics_payload,
    observe_request,
)
from app.db.session import get_db

from app.api.v1.appointments import router as appointments_router
from app.api.v1.analytics import router as analytics_router
from app.api.v1.auth import router as auth_router
from app.api.v1.departments import router as departments_router
from app.api.v1.services import router as services_router
from app.api.v1.providers import router as providers_router
from app.api.v1.provider_schedules import router as provider_schedules_router
from app.api.v1.visits import router as visits_router
from app.api.v1.waitlist import router as waitlist_router


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
            {
                "name": "appointments",
                "description": (
                    "Booking via the scheduling saga, plus cancel and "
                    "reschedule. Booking is idempotent on Idempotency-Key."
                ),
            },
            {
                "name": "waitlist",
                "description": (
                    "Queues for a provider's time. An entry is offered when a "
                    "slot is released."
                ),
            },
            {
                "name": "visits",
                "description": (
                    "CHECKED_IN to COMPLETED on the day. Every transition is "
                    "idempotent."
                ),
            },
        ],
    )

    register_exception_handlers(app)

    configure_logging()

    @app.middleware("http")
    async def add_correlation_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Give every request an id, and hand it back in the response.

        Reuses the caller's X-Request-ID when they send one, so a client
        retrying a booking can be traced across both attempts, and mints one
        otherwise. Returning it in the response header is what lets someone
        reporting a bug quote an id to grep for.

        The id is read back from the local variable rather than the
        ContextVar after call_next: Starlette runs the rest of the app in a
        child task, which inherits a *copy* of this context, so writes made
        downstream are not visible here. Reads downstream work fine, which
        is the only direction this design needs.

        Registered ahead of the routers so it also wraps error responses --
        a 404 or a 500 carries the header too, which is precisely when
        someone needs it.
        """
        correlation_id = set_correlation_id(request.headers.get(CORRELATION_ID_HEADER))
        response = await call_next(request)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response

    @app.middleware("http")
    async def record_http_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Count and time every request that passes through the app.

        Written here rather than in each router because the numbers
        Prometheus wants are identical for every endpoint, and anything that
        has to be remembered per-route eventually gets forgotten on one.

        /metrics skips itself deliberately. Prometheus scrapes it every 15
        seconds forever, so counting those would bury real traffic under
        scrapes within a day.

        The recording sits in a finally block so a request that blew up is
        still timed. A 500 that took 30 seconds is the most useful line on
        the whole page, and it is precisely the one an early return loses.
        If the app raised instead of returning, no status was ever set, and
        500 is the honest label for that.
        """
        if request.url.path == METRICS_PATH:
            return await call_next(request)

        started = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            observe_request(request.method, endpoint_label(request), status, started)

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(analytics_router, prefix="/api/v1")
    app.include_router(departments_router, prefix="/api/v1")
    app.include_router(services_router, prefix="/api/v1")
    app.include_router(providers_router, prefix="/api/v1")
    app.include_router(provider_schedules_router, prefix="/api/v1")
    app.include_router(appointments_router, prefix="/api/v1")
    app.include_router(waitlist_router, prefix="/api/v1")
    app.include_router(visits_router, prefix="/api/v1")

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Liveness: is this process up? Deliberately checks nothing else,
        so it stays fast and never fails because a dependency is slow."""
        return {"status": "ok", "env": settings.app_env}

    @app.get("/health/ready", tags=["health"])
    async def health_ready(
        db: Session = Depends(get_db),
        redis_client: Redis = Depends(get_redis),
    ) -> JSONResponse:
        """Readiness: are Postgres, Redis, Kafka and Temporal all reachable?

        503 rather than 500 when something is down. 500 means this service
        is broken; 503 means it is fine but cannot serve yet. A load
        balancer treats them differently -- 503 says stop sending traffic
        here for now, 500 says the deployment failed.

        Returns the per-dependency breakdown instead of the app's usual
        {"error": {...}} envelope, which is a deliberate exception to the
        one-error-shape rule. The reader here is a monitoring tool, not an
        API client, and "which one is down" is the entire reason to call
        this -- an envelope carrying a single message would throw away the
        only useful thing in the response. Dependency names only: never a
        host, a port or a connection string.
        """
        checks = await run_checks(db, redis_client)
        ready = all(verdict == "ok" for verdict in checks.values())
        return JSONResponse(
            status_code=(
                status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            content={"status": "ready" if ready else "not_ready", "checks": checks},
        )

    @app.get(METRICS_PATH, tags=["health"], include_in_schema=False)
    def metrics() -> Response:
        """The page Prometheus scrapes.

        Kept out of the OpenAPI schema: it is for a monitoring tool, not for
        anyone reading the API docs, and its body is not JSON like every
        other response here.
        """
        payload, content_type = metrics_payload()
        return Response(content=payload, media_type=content_type)

    return app


app = create_app()

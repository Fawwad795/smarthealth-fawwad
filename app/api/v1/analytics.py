"""Analytics routes: the six operational metrics, read from the aggregates.

Staff only. Nothing here is one patient's data -- these are clinic
operations figures -- so there is no per-patient scoping, only a role
check.

Two endpoints rather than six. A dashboard wants the summary in one call,
and the only metric that is genuinely a series over time is appointments
booked; six routes returning one number each would be more surface for
the same information.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import require_role
from app.db.session import get_db
from app.models import User
from app.models.enums import UserRole
from app.schemas.analytics import (
    AnalyticsSummaryResponse,
    AppointmentsSeriesResponse,
    DateRange,
    ReconciliationResponse,
)
from app.schemas.errors import error_responses
from app.services import analytics as analytics_service

router = APIRouter(prefix="/analytics", tags=["analytics"])

_ANALYTICS_ROLES = (UserRole.FRONT_DESK, UserRole.ADMIN)


@router.get(
    "/summary",
    response_model=AnalyticsSummaryResponse,
    summary="The six operational metrics for a date range",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Analytics are staff-only.",
        }
    ),
)
def get_summary(
    params: Annotated[DateRange, Query()],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ANALYTICS_ROLES)),
) -> AnalyticsSummaryResponse:
    """Read the six metrics for a date range. FRONT_DESK/ADMIN only.

    Defaults to the last 30 days. 422 if start_date is after end_date or
    the range exceeds 366 days. 403 for any other role.
    """
    summary = analytics_service.summarise_range(db, params.start_date, params.end_date)
    return AnalyticsSummaryResponse.model_validate(summary)


@router.get(
    "/appointments",
    response_model=AppointmentsSeriesResponse,
    summary="Appointments booked per day",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Analytics are staff-only.",
        }
    ),
)
def get_appointments_series(
    params: Annotated[DateRange, Query()],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*_ANALYTICS_ROLES)),
) -> AppointmentsSeriesResponse:
    """Appointments booked per day across a range. FRONT_DESK/ADMIN only.

    One entry per day, zero for days with no bookings. Defaults to the
    last 30 days. 422 on an invalid or oversized range, 403 for any other
    role.
    """
    buckets = analytics_service.appointments_series(
        db, params.start_date, params.end_date
    )
    return AppointmentsSeriesResponse.model_validate(
        {
            "start_date": params.start_date,
            "end_date": params.end_date,
            "buckets": buckets,
        }
    )


@router.get(
    "/reconciliation",
    response_model=ReconciliationResponse,
    summary="Compare the aggregates against the raw tables and report drift",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Reconciliation is ADMIN only.",
        }
    ),
)
def get_reconciliation(
    params: Annotated[DateRange, Query()],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.ADMIN)),
) -> ReconciliationResponse:
    """Report any day where analytics_daily disagrees with the raw tables.

    ADMIN only, one step tighter than the other two analytics routes.
    Those answer questions about the clinic; this one answers a question
    about whether our own pipeline is working, which is an operator's
    concern rather than a front desk's.

    Read-only. It never repairs what it finds -- scripts/reconcile_analytics.py
    --repair is the deliberate way to do that.
    """
    drifted = analytics_service.reconcile_range(db, params.start_date, params.end_date)
    return ReconciliationResponse(
        start_date=params.start_date,
        end_date=params.end_date,
        days_checked=(params.end_date - params.start_date).days + 1,
        in_sync=not drifted,
        drifted_days=drifted,
    )

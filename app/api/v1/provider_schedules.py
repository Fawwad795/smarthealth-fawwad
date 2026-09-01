"""Provider schedule routes: recurring weekly templates, and the action
that turns them into concrete bookable Slot rows.

Nested under /providers/{provider_id} because a schedule only ever makes
sense in the context of one provider -- there is no "list every schedule
across every provider" use case here.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.dependencies import require_role
from app.core.pagination import PaginationParams, pagination_params
from app.db.session import get_db
from app.models.enums import UserRole
from app.schemas.provider_schedule import (
    GenerateSlotsRequest,
    GenerateSlotsResponse,
    ProviderScheduleCreate,
    ProviderScheduleListResponse,
    ProviderScheduleResponse,
    ProviderScheduleUpdate,
)
from app.schemas.errors import error_responses
from app.services import provider_schedule as provider_schedule_service

router = APIRouter(
    prefix="/providers/{provider_id}/schedules", tags=["provider-schedules"]
)

_STAFF_ROLES = (UserRole.ADMIN, UserRole.FRONT_DESK, UserRole.PROVIDER)


@router.post(
    "",
    response_model=ProviderScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a weekly schedule template",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Admin only.",
            status.HTTP_404_NOT_FOUND: "PROVIDER_NOT_FOUND.",
            status.HTTP_409_CONFLICT: "DUPLICATE_SCHEDULE_WINDOW, or OVERLAPPING_SCHEDULE_WINDOWS on the same weekday.",
            status.HTTP_422_UNPROCESSABLE_ENTITY: "INVALID_SCHEDULE_WINDOW -- end_time is not after start_time.",
        }
    ),
)
def create_provider_schedule(
    provider_id: int,
    data: ProviderScheduleCreate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ProviderScheduleResponse:
    """Add one recurring weekly window to a provider's schedule. ADMIN only.

    Creates the template only -- nothing is bookable until
    /generate-slots turns it into Slot rows. 409 on a duplicate
    (weekday, start_time) window for this provider.
    """
    schedule = provider_schedule_service.create_provider_schedule(db, provider_id, data)
    return ProviderScheduleResponse.model_validate(schedule)


@router.get(
    "",
    response_model=ProviderScheduleListResponse,
    summary="List a provider's schedule templates",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Staff only.",
            status.HTTP_404_NOT_FOUND: "PROVIDER_NOT_FOUND.",
        }
    ),
)
def list_provider_schedules(
    provider_id: int,
    pagination: PaginationParams = Depends(pagination_params),
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ProviderScheduleListResponse:
    """List one provider's schedule windows, paginated. Any staff role.

    Includes retired (is_active=False) windows, so staff can see why
    historical slots exist.
    """
    items, total = provider_schedule_service.list_provider_schedules(
        db, provider_id, pagination
    )
    return ProviderScheduleListResponse(
        items=[ProviderScheduleResponse.model_validate(s) for s in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{schedule_id}",
    response_model=ProviderScheduleResponse,
    summary="Read one schedule template",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Staff only.",
            status.HTTP_404_NOT_FOUND: "SCHEDULE_NOT_FOUND -- including a schedule that exists but belongs to another provider.",
        }
    ),
)
def get_provider_schedule(
    provider_id: int,
    schedule_id: int,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ProviderScheduleResponse:
    """Fetch one schedule window. Any staff role.

    A schedule_id belonging to a different provider returns 404, not the
    row -- the path's provider_id is checked, not trusted.
    """
    schedule = provider_schedule_service.get_provider_schedule(
        db, provider_id, schedule_id
    )
    return ProviderScheduleResponse.model_validate(schedule)


@router.patch(
    "/{schedule_id}",
    response_model=ProviderScheduleResponse,
    summary="Update a schedule template",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Admin only.",
            status.HTTP_404_NOT_FOUND: "SCHEDULE_NOT_FOUND, including one belonging to another provider.",
            status.HTTP_409_CONFLICT: "DUPLICATE_SCHEDULE_WINDOW or OVERLAPPING_SCHEDULE_WINDOWS.",
            status.HTTP_422_UNPROCESSABLE_ENTITY: "INVALID_SCHEDULE_WINDOW.",
        }
    ),
)
def update_provider_schedule(
    provider_id: int,
    schedule_id: int,
    data: ProviderScheduleUpdate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ProviderScheduleResponse:
    """Edit a schedule window, or retire it with is_active=False. ADMIN only.

    Retiring stops future generation runs using it; slots already
    generated from it are untouched, since a booked slot must never
    vanish because someone edited a template.
    """
    schedule = provider_schedule_service.update_provider_schedule(
        db, provider_id, schedule_id, data
    )
    return ProviderScheduleResponse.model_validate(schedule)


@router.post(
    "/generate-slots",
    response_model=GenerateSlotsResponse,
    summary="Generate bookable slots from the templates (idempotent)",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Admin only.",
            status.HTTP_404_NOT_FOUND: "PROVIDER_NOT_FOUND.",
        }
    ),
)
def generate_slots(
    provider_id: int,
    data: GenerateSlotsRequest,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> GenerateSlotsResponse:
    """Generate bookable Slot rows from this provider's active schedules
    over a date range. ADMIN only.

    An action, not a resource create, so it returns 200 with counts
    rather than 201 with a body. Idempotent: re-running over a range
    that already has slots reports them as `skipped` instead of
    duplicating them, so widening the window is always safe.
    """
    created, skipped = provider_schedule_service.generate_slots(
        db, provider_id, data.start_date, data.end_date
    )
    return GenerateSlotsResponse(created=created, skipped=skipped)

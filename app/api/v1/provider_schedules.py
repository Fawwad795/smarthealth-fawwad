"""Provider schedule routes: recurring weekly templates, and the action
that turns them into concrete bookable Slot rows.

Nested under /providers/{provider_id} because a schedule only ever makes
sense in the context of one provider -- there is no "list every schedule
across every provider" use case here.
"""

from fastapi import APIRouter, Depends
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
from app.services import provider_schedule as provider_schedule_service

router = APIRouter(prefix="/providers/{provider_id}/schedules", tags=["provider-schedules"])

_STAFF_ROLES = (UserRole.ADMIN, UserRole.FRONT_DESK, UserRole.PROVIDER)


@router.post("", response_model=ProviderScheduleResponse, status_code=201)
def create_provider_schedule(
    provider_id: int,
    data: ProviderScheduleCreate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ProviderScheduleResponse:
    schedule = provider_schedule_service.create_provider_schedule(db, provider_id, data)
    return ProviderScheduleResponse.model_validate(schedule)


@router.get("", response_model=ProviderScheduleListResponse)
def list_provider_schedules(
    provider_id: int,
    pagination: PaginationParams = Depends(pagination_params),
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ProviderScheduleListResponse:
    items, total = provider_schedule_service.list_provider_schedules(
        db, provider_id, pagination
    )
    return ProviderScheduleListResponse(
        items=[ProviderScheduleResponse.model_validate(s) for s in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/{schedule_id}", response_model=ProviderScheduleResponse)
def get_provider_schedule(
    provider_id: int,
    schedule_id: int,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ProviderScheduleResponse:
    schedule = provider_schedule_service.get_provider_schedule(
        db, provider_id, schedule_id
    )
    return ProviderScheduleResponse.model_validate(schedule)


@router.patch("/{schedule_id}", response_model=ProviderScheduleResponse)
def update_provider_schedule(
    provider_id: int,
    schedule_id: int,
    data: ProviderScheduleUpdate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ProviderScheduleResponse:
    schedule = provider_schedule_service.update_provider_schedule(
        db, provider_id, schedule_id, data
    )
    return ProviderScheduleResponse.model_validate(schedule)


@router.post("/generate-slots", response_model=GenerateSlotsResponse)
def generate_slots(
    provider_id: int,
    data: GenerateSlotsRequest,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> GenerateSlotsResponse:
    created, skipped = provider_schedule_service.generate_slots(
        db, provider_id, data.start_date, data.end_date
    )
    return GenerateSlotsResponse(created=created, skipped=skipped)

"""Service routes: admin manages services, staff can read them.

Every service here is DRAFT and stays DRAFT -- status and published_at are
never accepted from a request body. Week 2's Temporal publish workflow is
the only path to PUBLISHED; letting a PATCH set status directly would let
a client skip validation and chunking entirely.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import require_role
from app.core.pagination import PaginationParams, pagination_params
from app.db.session import get_db
from app.models.enums import UserRole
from app.schemas.service import (
    ServiceCreate,
    ServiceListResponse,
    ServiceResponse,
    ServiceUpdate,
)
from app.services import service as service_service

router = APIRouter(prefix="/services", tags=["services"])

_STAFF_ROLES = (UserRole.ADMIN, UserRole.FRONT_DESK, UserRole.PROVIDER)


@router.post("", response_model=ServiceResponse, status_code=201)
def create_service(
    data: ServiceCreate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ServiceResponse:
    service = service_service.create_service(db, data)
    return ServiceResponse.model_validate(service)


@router.get("", response_model=ServiceListResponse)
def list_services(
    pagination: PaginationParams = Depends(pagination_params),
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ServiceListResponse:
    items, total = service_service.list_services(db, pagination)
    return ServiceListResponse(
        items=[ServiceResponse.model_validate(s) for s in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/{service_id}", response_model=ServiceResponse)
def get_service(
    service_id: int,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ServiceResponse:
    service = service_service.get_service(db, service_id)
    return ServiceResponse.model_validate(service)


@router.patch("/{service_id}", response_model=ServiceResponse)
def update_service(
    service_id: int,
    data: ServiceUpdate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ServiceResponse:
    service = service_service.update_service(db, service_id, data)
    return ServiceResponse.model_validate(service)

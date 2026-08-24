"""Provider routes: admin manages provider profiles, staff can read them."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import require_role
from app.core.pagination import PaginationParams, pagination_params
from app.db.session import get_db
from app.models.enums import UserRole
from app.schemas.provider import (
    ProviderCreate,
    ProviderListResponse,
    ProviderResponse,
    ProviderUpdate,
)
from app.services import provider as provider_service

router = APIRouter(prefix="/providers", tags=["providers"])

_STAFF_ROLES = (UserRole.ADMIN, UserRole.FRONT_DESK, UserRole.PROVIDER)


@router.post("", response_model=ProviderResponse, status_code=201)
def create_provider(
    data: ProviderCreate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ProviderResponse:
    provider = provider_service.create_provider(db, data)
    return ProviderResponse.model_validate(provider)


@router.get("", response_model=ProviderListResponse)
def list_providers(
    pagination: PaginationParams = Depends(pagination_params),
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ProviderListResponse:
    items, total = provider_service.list_providers(db, pagination)
    return ProviderListResponse(
        items=[ProviderResponse.model_validate(p) for p in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/{provider_id}", response_model=ProviderResponse)
def get_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ProviderResponse:
    provider = provider_service.get_provider(db, provider_id)
    return ProviderResponse.model_validate(provider)


@router.patch("/{provider_id}", response_model=ProviderResponse)
def update_provider(
    provider_id: int,
    data: ProviderUpdate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ProviderResponse:
    provider = provider_service.update_provider(db, provider_id, data)
    return ProviderResponse.model_validate(provider)

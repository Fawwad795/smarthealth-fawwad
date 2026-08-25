"""Provider routes: admin manages provider profiles, staff can read them."""

from fastapi import APIRouter, Depends, status
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


@router.post("", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
def create_provider(
    data: ProviderCreate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ProviderResponse:
    """Attach a provider profile to an existing PROVIDER account. ADMIN only.

    Does not create the account itself. 404 if the user_id is unknown,
    409 if that user isn't role PROVIDER or already has a profile.
    """
    provider = provider_service.create_provider(db, data)
    return ProviderResponse.model_validate(provider)


@router.get("", response_model=ProviderListResponse)
def list_providers(
    pagination: PaginationParams = Depends(pagination_params),
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ProviderListResponse:
    """List provider profiles, paginated. Any staff role."""
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
    """Fetch one provider profile by id. Any staff role.

    provider_id is the Provider row's id, not the user_id of the account
    behind it.
    """
    provider = provider_service.get_provider(db, provider_id)
    return ProviderResponse.model_validate(provider)


@router.patch("/{provider_id}", response_model=ProviderResponse)
def update_provider(
    provider_id: int,
    data: ProviderUpdate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ProviderResponse:
    """Move a provider between departments/specialties, or edit their bio.
    ADMIN only.

    user_id cannot be changed -- ProviderUpdate has no field for it.
    """
    provider = provider_service.update_provider(db, provider_id, data)
    return ProviderResponse.model_validate(provider)

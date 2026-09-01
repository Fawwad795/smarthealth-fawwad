"""Service routes: admin manages services, staff can read them, and one
public route lets anyone browse the published catalogue.

Every service created here is DRAFT and stays DRAFT -- status and
published_at are never accepted from a request body. Week 2's Temporal
publish workflow is the only path to PUBLISHED; letting a PATCH set
status directly would let a client skip validation and chunking entirely.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.dependencies import require_role
from app.core.pagination import PaginationParams, pagination_params
from app.db.session import get_db
from app.models.enums import UserRole
from app.schemas.service import (
    ServiceCreate,
    ServiceListResponse,
    ServicePublishStatusResponse,
    ServiceResponse,
    ServiceUpdate,
)
from app.schemas.service_search import (
    PublicServiceListResponse,
    ServiceSearchParams,
    service_search_params,
)
from app.schemas.errors import error_responses
from app.services import service_search as service_search_service
from app.services import service as service_service
from app.services import service_publish

router = APIRouter(prefix="/services", tags=["services"])

_STAFF_ROLES = (UserRole.ADMIN, UserRole.FRONT_DESK, UserRole.PROVIDER)


@router.post(
    "",
    response_model=ServiceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a service (always DRAFT)",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Admin only.",
            status.HTTP_404_NOT_FOUND: "DEPARTMENT_NOT_FOUND.",
            status.HTTP_409_CONFLICT: "SERVICE_NAME_TAKEN within this department.",
        }
    ),
)
def create_service(
    data: ServiceCreate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ServiceResponse:
    """Create a service, always in DRAFT. ADMIN only.

    A `status` key in the request body is silently ignored: ServiceCreate
    has no such field for it to bind to.
    """
    service = service_service.create_service(db, data)
    return ServiceResponse.model_validate(service)


@router.get(
    "",
    response_model=ServiceListResponse,
    summary="List services in any status",
    responses=error_responses({status.HTTP_403_FORBIDDEN: "Staff only."}),
)
def list_services(
    pagination: PaginationParams = Depends(pagination_params),
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ServiceListResponse:
    """List services in any status, paginated. Any staff role.

    The staff view. Patients get /services/search below, which is
    filtered to PUBLISHED.
    """
    items, total = service_service.list_services(db, pagination)
    return ServiceListResponse(
        items=[ServiceResponse.model_validate(s) for s in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/search",
    response_model=PublicServiceListResponse,
    summary="Browse the published catalogue (public)",
)
def search_services(
    search: ServiceSearchParams = Depends(service_search_params),
    pagination: PaginationParams = Depends(pagination_params),
    db: Session = Depends(get_db),
) -> PublicServiceListResponse:
    """Browse the published service catalogue. Public -- no auth required.

    No require_role dependency, deliberately: a prospective patient
    browsing before they even register is the whole point of this route.
    Only PUBLISHED services are ever returned, enforced in SQL.

    Declared here, before GET /{service_id} below, on purpose: Starlette
    matches routes in declaration order, and /{service_id} would otherwise
    swallow "/search" as an (invalid) service_id and 422 before this route
    is ever reached.
    """
    items, total = service_search_service.search_services(db, search, pagination)
    return PublicServiceListResponse(
        items=items,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{service_id}",
    response_model=ServiceResponse,
    summary="Read one service",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Staff only -- a DRAFT service is internal.",
            status.HTTP_404_NOT_FOUND: "SERVICE_NOT_FOUND.",
        }
    ),
)
def get_service(
    service_id: int,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ServiceResponse:
    """Fetch one service by id, in any status. Any staff role.

    Staff-only because a DRAFT service is internal -- it describes
    something the clinic does not yet offer.
    """
    service = service_service.get_service(db, service_id)
    return ServiceResponse.model_validate(service)


@router.patch(
    "/{service_id}",
    response_model=ServiceResponse,
    summary="Update a service (never its status)",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Admin only.",
            status.HTTP_404_NOT_FOUND: "SERVICE_NOT_FOUND.",
            status.HTTP_409_CONFLICT: "SERVICE_NAME_TAKEN within this department.",
        }
    ),
)
def update_service(
    service_id: int,
    data: ServiceUpdate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ServiceResponse:
    """Partially update a service's name, description or prep
    instructions. ADMIN only.

    Cannot change status: ServiceUpdate has no such field, so a client
    sending one gets its name updated and its status left alone.
    """
    service = service_service.update_service(db, service_id, data)
    return ServiceResponse.model_validate(service)


@router.post(
    "/{service_id}/publish",
    response_model=ServicePublishStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start the publish workflow",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Admin only.",
            status.HTTP_404_NOT_FOUND: "SERVICE_NOT_FOUND.",
            status.HTTP_409_CONFLICT: "SERVICE_NOT_PUBLISHABLE -- not DRAFT or PUBLISH_FAILED.",
        }
    ),
)
async def publish_service(
    service_id: int,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> ServicePublishStatusResponse:
    """Start the publish workflow for a service. ADMIN only.

    202: the workflow has started, not necessarily finished -- poll GET
    publish-status for the outcome. 404 if the service doesn't exist, 409
    if it isn't DRAFT or PUBLISH_FAILED.
    """
    service, workflow_id = await service_publish.start_publish(db, service_id)
    return ServicePublishStatusResponse(
        service_id=service.id,
        status=service.status,
        published_at=service.published_at,
        workflow_id=workflow_id,
    )


@router.get(
    "/{service_id}/publish-status",
    response_model=ServicePublishStatusResponse,
    summary="Read where the publish lifecycle stands",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Staff only.",
            status.HTTP_404_NOT_FOUND: "SERVICE_NOT_FOUND.",
        }
    ),
)
def get_publish_status(
    service_id: int,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> ServicePublishStatusResponse:
    """Report where this service's publish lifecycle currently stands.
    Any staff role. 404 if the service doesn't exist.

    Reads status/published_at straight off the service row -- the
    workflow's Activities keep both in sync at every step, so this needs
    no live query into Temporal itself.
    """
    service = service_service.get_service(db, service_id)
    return ServicePublishStatusResponse(
        service_id=service.id,
        status=service.status,
        published_at=service.published_at,
        workflow_id=service_publish.publish_workflow_id(service.id),
    )

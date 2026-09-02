"""Department routes: admin manages departments, staff can read them.

Read access is any staff role (not PATIENT) -- departments are internal
organisational structure, not something a patient browses directly. Task
1.8's public listing is the patient-facing view, built on Service and
Provider, not this endpoint.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.dependencies import require_role
from app.core.pagination import PaginationParams, pagination_params
from app.db.session import get_db
from app.models.enums import UserRole
from app.schemas.department import (
    DepartmentCreate,
    DepartmentListResponse,
    DepartmentResponse,
    DepartmentUpdate,
)
from app.schemas.errors import error_responses
from app.services import department as department_service

router = APIRouter(prefix="/departments", tags=["departments"])

_STAFF_ROLES = (UserRole.ADMIN, UserRole.FRONT_DESK, UserRole.PROVIDER)


@router.post(
    "",
    response_model=DepartmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a department",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Admin only.",
            status.HTTP_404_NOT_FOUND: "CLINIC_NOT_FOUND.",
            status.HTTP_409_CONFLICT: "DEPARTMENT_NAME_TAKEN within this clinic.",
        }
    ),
)
def create_department(
    data: DepartmentCreate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> DepartmentResponse:
    """Create a department. ADMIN only.

    409 if this clinic already has a department by this name (compared
    case-insensitively), 404 if the clinic_id doesn't exist.
    """
    department = department_service.create_department(db, data)
    return DepartmentResponse.model_validate(department)


@router.get(
    "",
    response_model=DepartmentListResponse,
    summary="List departments",
    responses=error_responses({status.HTTP_403_FORBIDDEN: "Staff only."}),
)
def list_departments(
    pagination: PaginationParams = Depends(pagination_params),
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> DepartmentListResponse:
    """List departments, paginated. Any staff role.

    Accepts ?limit= and ?offset=; limit is capped at 100 by
    pagination_params so a client cannot ask for the whole table.
    """
    items, total = department_service.list_departments(db, pagination)
    return DepartmentListResponse(
        items=[DepartmentResponse.model_validate(d) for d in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{department_id}",
    response_model=DepartmentResponse,
    summary="Read one department",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Staff only.",
            status.HTTP_404_NOT_FOUND: "DEPARTMENT_NOT_FOUND.",
        }
    ),
)
def get_department(
    department_id: int,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> DepartmentResponse:
    """Fetch one department by id. Any staff role. 404 if it doesn't exist."""
    department = department_service.get_department(db, department_id)
    return DepartmentResponse.model_validate(department)


@router.patch(
    "/{department_id}",
    response_model=DepartmentResponse,
    summary="Update a department",
    responses=error_responses(
        {
            status.HTTP_403_FORBIDDEN: "Admin only.",
            status.HTTP_404_NOT_FOUND: "DEPARTMENT_NOT_FOUND.",
            status.HTTP_409_CONFLICT: "DEPARTMENT_NAME_TAKEN within this clinic.",
        }
    ),
)
def update_department(
    department_id: int,
    data: DepartmentUpdate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> DepartmentResponse:
    """Partially update a department. ADMIN only.

    PATCH, not PUT: any field omitted from the body is left untouched
    rather than cleared.
    """
    department = department_service.update_department(db, department_id, data)
    return DepartmentResponse.model_validate(department)

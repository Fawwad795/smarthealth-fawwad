"""Department routes: admin manages departments, staff can read them.

Read access is any staff role (not PATIENT) -- departments are internal
organisational structure, not something a patient browses directly. Task
1.8's public listing is the patient-facing view, built on Service and
Provider, not this endpoint.
"""

from fastapi import APIRouter, Depends
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
from app.services import department as department_service

router = APIRouter(prefix="/departments", tags=["departments"])

_STAFF_ROLES = (UserRole.ADMIN, UserRole.FRONT_DESK, UserRole.PROVIDER)


@router.post("", response_model=DepartmentResponse, status_code=201)
def create_department(
    data: DepartmentCreate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> DepartmentResponse:
    department = department_service.create_department(db, data)
    return DepartmentResponse.model_validate(department)


@router.get("", response_model=DepartmentListResponse)
def list_departments(
    pagination: PaginationParams = Depends(pagination_params),
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> DepartmentListResponse:
    items, total = department_service.list_departments(db, pagination)
    return DepartmentListResponse(
        items=[DepartmentResponse.model_validate(d) for d in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/{department_id}", response_model=DepartmentResponse)
def get_department(
    department_id: int,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(*_STAFF_ROLES)),
) -> DepartmentResponse:
    department = department_service.get_department(db, department_id)
    return DepartmentResponse.model_validate(department)


@router.patch("/{department_id}", response_model=DepartmentResponse)
def update_department(
    department_id: int,
    data: DepartmentUpdate,
    db: Session = Depends(get_db),
    _current_user=Depends(require_role(UserRole.ADMIN)),
) -> DepartmentResponse:
    department = department_service.update_department(db, department_id, data)
    return DepartmentResponse.model_validate(department)
"""Business rules for department management: create, list, get, update.

Departments are master data an admin manages directly -- there is no
lifecycle here the way there is for a Service (DRAFT/PUBLISHED) or a Slot
(AVAILABLE/BOOKED). Just rows that must stay valid.
"""

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.pagination import PaginationParams
from app.models import Department
from app.schemas.department import DepartmentCreate, DepartmentUpdate


def create_department(db: Session, data: DepartmentCreate) -> Department:
    """Insert a department, or fail with 409 if this clinic already has one
    by this name.

    Checked with a query first, matching auth's register_patient, rather
    than relying only on the unique constraint -- a bare IntegrityError
    here can't tell "duplicate name" apart from "clinic_id doesn't exist",
    and those deserve different status codes.
    """
    exists = db.execute(
        select(Department).where(
            Department.clinic_id == data.clinic_id,
            func.lower(Department.name) == data.name.lower(),
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="DEPARTMENT_NAME_TAKEN",
            message="This clinic already has a department with this name.",
        )

    department = Department(
        clinic_id=data.clinic_id,
        name=data.name,
        order_index=data.order_index,
    )
    db.add(department)
    try:
        db.commit()
    except IntegrityError:
        # The clinic_id foreign key is the only other constraint this
        # insert can violate -- the name check above already ruled out
        # the unique constraint.
        db.rollback()
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="CLINIC_NOT_FOUND",
            message="No clinic exists with this clinic_id.",
        )
    db.refresh(department)
    return department


def get_department(db: Session, department_id: int) -> Department:
    """Fetch one department by id, or raise 404.

    Every caller that needs a department starts here rather than calling
    db.get() itself, so "missing" produces one consistent error shape
    instead of a None that each caller has to remember to check.
    """
    department = db.get(Department, department_id)
    if department is None:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="DEPARTMENT_NOT_FOUND",
            message="No department exists with this id.",
        )
    return department


def list_departments(
    db: Session, pagination: PaginationParams
) -> tuple[list[Department], int]:
    """Return one page of departments plus the unpaginated total.

    Two queries on purpose: a single one cannot report both "these 20 rows"
    and "how many exist in total", and the caller needs the total to know
    how many pages there are.

    Ordered explicitly, because without ORDER BY Postgres may return rows
    in a different order per query -- which would let page 2 repeat or skip
    rows already seen on page 1.
    """
    total = db.execute(select(func.count()).select_from(Department)).scalar_one()
    items = (
        db.execute(
            select(Department)
            .order_by(Department.clinic_id, Department.order_index, Department.id)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        .scalars()
        .all()
    )
    return list(items), total


def update_department(
    db: Session, department_id: int, data: DepartmentUpdate
) -> Department:
    """Apply a partial update, or raise 404 (unknown id) / 409 (name clash).

    A field left as None on the request schema means "not mentioned, leave
    it alone" -- never "set this to null". Neither column is nullable, so
    there is no way to ask for that and no ambiguity in reading it.
    """
    department = get_department(db, department_id)

    if data.name is not None:
        exists = db.execute(
            select(Department).where(
                Department.clinic_id == department.clinic_id,
                func.lower(Department.name) == data.name.lower(),
                Department.id != department.id,
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise AppError(
                status_code=status.HTTP_409_CONFLICT,
                code="DEPARTMENT_NAME_TAKEN",
                message="This clinic already has a department with this name.",
            )
        department.name = data.name

    if data.order_index is not None:
        department.order_index = data.order_index

    db.commit()
    db.refresh(department)
    return department

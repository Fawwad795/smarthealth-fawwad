"""Business rules for service management: create, list, get, update.

A service is created as DRAFT and stays there until Week 2's Temporal
publish workflow promotes it -- this module never writes status or
published_at. Those columns exist on the model already; this is
deliberately the only piece of code in Week 1 that reads them and the
only one that must never write them.
"""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.pagination import PaginationParams
from app.models import Service
from app.schemas.service import ServiceCreate, ServiceUpdate


def create_service(db: Session, data: ServiceCreate) -> Service:
    """Insert a service in DRAFT, or fail with 409 if this department
    already has one by this name.
    """
    exists = db.execute(
        select(Service).where(
            Service.department_id == data.department_id,
            func.lower(Service.name) == data.name.lower(),
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise AppError(
            status_code=409,
            code="SERVICE_NAME_TAKEN",
            message="This department already has a service with this name.",
        )

    service = Service(
        department_id=data.department_id,
        name=data.name,
        description=data.description,
        prep_instructions=data.prep_instructions,
    )
    db.add(service)
    try:
        db.commit()
    except IntegrityError:
        # The department_id foreign key is the only other constraint this
        # insert can violate -- the name check above already ruled out
        # the unique constraint.
        db.rollback()
        raise AppError(
            status_code=404,
            code="DEPARTMENT_NOT_FOUND",
            message="No department exists with this department_id.",
        )
    db.refresh(service)
    return service


def get_service(db: Session, service_id: int) -> Service:
    service = db.get(Service, service_id)
    if service is None:
        raise AppError(
            status_code=404,
            code="SERVICE_NOT_FOUND",
            message="No service exists with this id.",
        )
    return service


def list_services(
    db: Session, pagination: PaginationParams
) -> tuple[list[Service], int]:
    total = db.execute(select(func.count()).select_from(Service)).scalar_one()
    items = (
        db.execute(
            select(Service)
            .order_by(Service.department_id, Service.name, Service.id)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        .scalars()
        .all()
    )
    return list(items), total


def update_service(db: Session, service_id: int, data: ServiceUpdate) -> Service:
    service = get_service(db, service_id)

    if data.name is not None:
        exists = db.execute(
            select(Service).where(
                Service.department_id == service.department_id,
                func.lower(Service.name) == data.name.lower(),
                Service.id != service.id,
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise AppError(
                status_code=409,
                code="SERVICE_NAME_TAKEN",
                message="This department already has a service with this name.",
            )
        service.name = data.name

    if data.description is not None:
        service.description = data.description

    if data.prep_instructions is not None:
        service.prep_instructions = data.prep_instructions

    db.commit()
    db.refresh(service)
    return service

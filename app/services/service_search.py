"""Public service catalog search: published, offered services only.

Every filter here runs in the SQL WHERE clause, never client-side and
never inside a prompt -- CLAUDE.md 9.3 requires exactly this (published +
offered) filtering for Week 4's retrieval logic too, so getting it right
here is not just today's task.
"""

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.pagination import PaginationParams
from app.models import Department, Provider, ProviderService, Service, Slot, Specialty
from app.models.enums import ServiceStatus, SlotStatus
from app.schemas.service_search import ServiceSearchParams


def search_services(
    db: Session, params: ServiceSearchParams, pagination: PaginationParams
) -> tuple[list[dict], int]:
    """Every optional filter is an EXISTS subquery correlated back to
    Service.id, not a JOIN -- a service offered by three providers would
    otherwise appear three times, and de-duplicating that after the fact
    is exactly the kind of bug that's invisible until someone notices the
    total count is wrong.
    """
    filters = [Service.status == ServiceStatus.PUBLISHED]

    if params.q:
        filters.append(Service.name.ilike(f"%{params.q}%"))
    if params.department_id is not None:
        filters.append(Service.department_id == params.department_id)
    if params.specialty_id is not None:
        filters.append(
            exists(
                select(ProviderService.id)
                .join(Provider, Provider.id == ProviderService.provider_id)
                .where(
                    ProviderService.service_id == Service.id,
                    Provider.specialty_id == params.specialty_id,
                )
            )
        )
    if params.has_available_slots:
        filters.append(
            exists(
                select(ProviderService.id)
                .join(Slot, Slot.provider_id == ProviderService.provider_id)
                .where(
                    ProviderService.service_id == Service.id,
                    Slot.status == SlotStatus.AVAILABLE,
                    Slot.start_time > func.now(),
                )
            )
        )

    total = db.execute(
        select(func.count()).select_from(Service).where(*filters)
    ).scalar_one()

    services = (
        db.execute(
            select(Service)
            .where(*filters)
            .order_by(Service.name, Service.id)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        .scalars()
        .all()
    )

    if not services:
        return [], total

    # Two targeted queries for just this page's rows, not one query per
    # service -- keeps this at O(1) queries regardless of page size.
    department_ids = {s.department_id for s in services}
    department_names = {
        d.id: d.name
        for d in db.execute(
            select(Department).where(Department.id.in_(department_ids))
        ).scalars().all()
    }

    service_ids = [s.id for s in services]
    specialties_by_service: dict[int, list[str]] = {sid: [] for sid in service_ids}
    for service_id, specialty_name in db.execute(
        select(ProviderService.service_id, Specialty.name)
        .join(Provider, Provider.id == ProviderService.provider_id)
        .join(Specialty, Specialty.id == Provider.specialty_id)
        .where(ProviderService.service_id.in_(service_ids))
        .distinct()
    ).all():
        specialties_by_service[service_id].append(specialty_name)

    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "prep_instructions": s.prep_instructions,
            "department_id": s.department_id,
            "department_name": department_names[s.department_id],
            "specialties": sorted(specialties_by_service[s.id]),
        }
        for s in services
    ], total

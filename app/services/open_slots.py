"""Public open-slot listing: the free, future slots a patient can book for
one published service.

The step between finding a service and booking it. Service search finds
the service; this lists when its providers can see the patient, with the
ids POST /appointments takes. Like the catalogue, it is public, and every
rule is in the SQL WHERE clause.
"""

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.pagination import PaginationParams
from app.models import Provider, ProviderService, Service, Slot, Specialty
from app.models.enums import ServiceStatus, SlotStatus


def list_open_slots(
    db: Session,
    service_id: int,
    provider_id: int | None,
    pagination: PaginationParams,
) -> tuple[list[dict], int]:
    """One page of the service's open slots, earliest first, plus the
    unpaginated total.

    A slot is provider time, not service time, so a service's slots are
    those of the providers who offer it (provider_services). Open means
    AVAILABLE and not yet started, the same rule as search's
    has_available_slots, so a service that filter finds always has slots
    here. provider_services is unique on (provider, service), so the join
    gives each slot once.

    A service that is not PUBLISHED is a 404, the same as one that does
    not exist, so the response never confirms that a draft exists.
    """
    service = db.get(Service, service_id)
    if service is None or service.status != ServiceStatus.PUBLISHED:
        raise AppError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="SERVICE_NOT_FOUND",
            message="No published service exists with this id.",
        )

    filters = [
        ProviderService.service_id == service_id,
        Slot.status == SlotStatus.AVAILABLE,
        Slot.start_time > func.now(),
    ]
    if provider_id is not None:
        filters.append(Slot.provider_id == provider_id)

    total = db.execute(
        select(func.count())
        .select_from(Slot)
        .join(ProviderService, ProviderService.provider_id == Slot.provider_id)
        .where(*filters)
    ).scalar_one()

    rows = db.execute(
        select(
            Slot.id, Slot.provider_id, Specialty.name, Slot.start_time, Slot.end_time
        )
        .select_from(Slot)
        .join(ProviderService, ProviderService.provider_id == Slot.provider_id)
        .join(Provider, Provider.id == Slot.provider_id)
        .join(Specialty, Specialty.id == Provider.specialty_id)
        .where(*filters)
        .order_by(Slot.start_time, Slot.id)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).all()

    return [
        {
            "slot_id": slot_id,
            "provider_id": slot_provider_id,
            "specialty": specialty,
            "start_time": start_time,
            "end_time": end_time,
        }
        for slot_id, slot_provider_id, specialty, start_time, end_time in rows
    ], total

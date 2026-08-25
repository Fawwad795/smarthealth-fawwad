"""Public service catalog: the patient-facing search over published,
offered services. Distinct from service.py's staff CRUD schemas -- this
is a read-only, aggregated shape built for browsing, not editing.
"""

from dataclasses import dataclass

from fastapi import Query
from pydantic import BaseModel


@dataclass
class ServiceSearchParams:
    """The optional filters a catalogue search accepts.

    A plain dataclass, not a Pydantic model, for the same reason
    PaginationParams is: the service layer takes one of these and tests
    build them directly, so it must carry no FastAPI machinery.

    Every field being None/False means "no filter" -- the published-only
    rule is not represented here because it is not optional.
    """

    q: str | None = None
    department_id: int | None = None
    specialty_id: int | None = None
    has_available_slots: bool = False


def service_search_params(
    q: str | None = Query(default=None, min_length=1, max_length=150),
    department_id: int | None = Query(default=None),
    specialty_id: int | None = Query(default=None),
    has_available_slots: bool = Query(default=False),
) -> ServiceSearchParams:
    """The FastAPI dependency -- split from ServiceSearchParams itself for
    the same reason as pagination_params: Query(...) objects only resolve
    to real values inside a request FastAPI is handling.
    """
    return ServiceSearchParams(
        q=q,
        department_id=department_id,
        specialty_id=specialty_id,
        has_available_slots=has_available_slots,
    )


class PublicServiceResponse(BaseModel):
    """One published service as a patient sees it.

    Carries department_name and specialty names rather than bare ids,
    because the caller here may be unauthenticated and has no other
    endpoint to resolve those ids against.

    No status field: everything in this response is PUBLISHED by
    construction, so returning it would say nothing.
    """

    id: int
    name: str
    description: str | None
    prep_instructions: str | None
    department_id: int
    department_name: str
    specialties: list[str]


class PublicServiceListResponse(BaseModel):
    """One page of catalogue results in the shared pagination envelope.

    total counts everything matching the filters, not everything in the
    services table -- so it shrinks as filters are added.
    """

    items: list[PublicServiceResponse]
    total: int
    limit: int
    offset: int

"""Public service catalog: the patient-facing search over published,
offered services. Distinct from service.py's staff CRUD schemas -- this
is a read-only, aggregated shape built for browsing, not editing.
"""

from dataclasses import dataclass

from fastapi import Query
from pydantic import BaseModel


@dataclass
class ServiceSearchParams:
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
    id: int
    name: str
    description: str | None
    prep_instructions: str | None
    department_id: int
    department_name: str
    specialties: list[str]


class PublicServiceListResponse(BaseModel):
    items: list[PublicServiceResponse]
    total: int
    limit: int
    offset: int

"""Service request/response schemas.

Deliberately no status or published_at on the writable schemas: those
columns are owned by Week 2's Temporal publish workflow, not by this CRUD
surface. ServiceResponse exposes them read-only so a client can see the
current lifecycle state without ever being able to set it directly.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ServiceStatus


class ServiceCreate(BaseModel):
    department_id: int
    name: str = Field(min_length=1, max_length=150)
    description: str | None = None
    prep_instructions: str | None = None


class ServiceUpdate(BaseModel):
    """Every field optional -- same "None means untouched" convention as
    DepartmentUpdate. status is not here: it is never writable through
    this endpoint.
    """

    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = None
    prep_instructions: str | None = None


class ServiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    department_id: int
    name: str
    description: str | None
    prep_instructions: str | None
    status: ServiceStatus
    published_at: datetime | None


class ServiceListResponse(BaseModel):
    items: list[ServiceResponse]
    total: int
    limit: int
    offset: int
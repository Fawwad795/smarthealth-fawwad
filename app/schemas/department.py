"""Department request/response schemas."""

from pydantic import BaseModel, ConfigDict, Field


class DepartmentCreate(BaseModel):
    clinic_id: int
    name: str = Field(min_length=1, max_length=100)
    order_index: int = Field(default=0, ge=0)


class DepartmentUpdate(BaseModel):
    """Every field optional: a PATCH may touch just one of them. A field
    left out of the request body stays untouched -- neither column is
    nullable, so there is no way to ask for "set this to nothing".
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    order_index: int | None = Field(default=None, ge=0)


class DepartmentResponse(BaseModel):
    # from_attributes lets this be built straight from the ORM object:
    # DepartmentResponse.model_validate(department_row).
    model_config = ConfigDict(from_attributes=True)

    id: int
    clinic_id: int
    name: str
    order_index: int


class DepartmentListResponse(BaseModel):
    items: list[DepartmentResponse]
    total: int
    limit: int
    offset: int
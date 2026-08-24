"""Provider request/response schemas."""

from pydantic import BaseModel, ConfigDict


class ProviderCreate(BaseModel):
    user_id: int
    department_id: int
    specialty_id: int
    bio: str | None = None


class ProviderUpdate(BaseModel):
    """No user_id here, deliberately. Moving a provider profile onto a
    different user account isn't an edit -- it means the wrong account got
    made a provider in the first place, which is a delete-and-recreate, not
    a PATCH.
    """

    department_id: int | None = None
    specialty_id: int | None = None
    bio: str | None = None


class ProviderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    department_id: int
    specialty_id: int
    bio: str | None


class ProviderListResponse(BaseModel):
    items: list[ProviderResponse]
    total: int
    limit: int
    offset: int

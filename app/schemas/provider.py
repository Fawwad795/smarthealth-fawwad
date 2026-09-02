"""Provider request/response schemas."""

from pydantic import BaseModel, ConfigDict


class ProviderCreate(BaseModel):
    """Attaches an operational profile to an account that already exists.

    user_id must belong to a user whose role is already PROVIDER -- this
    schema cannot express "make this person a provider", and the service
    layer rejects a user_id that isn't one.
    """

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
    """One provider profile as returned to a client.

    Ids only for department and specialty -- resolving them to names is
    the caller's job here. The public catalogue (PublicServiceResponse)
    is the place that does that resolution, because a patient browsing
    services has no way to look an id up.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    department_id: int
    specialty_id: int
    bio: str | None


class ProviderListResponse(BaseModel):
    """One page of provider profiles in the shared pagination envelope."""

    items: list[ProviderResponse]
    total: int
    limit: int
    offset: int

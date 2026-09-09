"""Waitlist request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import WaitlistStatus


class WaitlistJoin(BaseModel):
    """A request to join one provider's queue.

    patient_id is optional for the same reason as on AppointmentCreate: a
    PATIENT joins for themselves and it is derived from the token, never
    read from the body.
    """

    provider_id: int
    patient_id: int | None = None


class WaitlistResponse(BaseModel):
    """One place in a queue.

    created_at is exposed because it *is* the queue position -- entries
    are offered oldest first, so a caller can see where they stand.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_id: int
    patient_id: int
    status: WaitlistStatus
    created_at: datetime

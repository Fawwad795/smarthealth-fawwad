"""GET /services/{service_id}/slots: the open slots a patient can book for
one published service. Read-only, and public like the catalogue search.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class OpenSlotResponse(BaseModel):
    """One open slot. With the service id from the path, it carries every
    id POST /appointments needs.

    The provider is described by specialty because providers have no
    display name, and an id alone tells a patient nothing.
    """

    slot_id: int
    provider_id: int
    specialty: str
    start_time: datetime = Field(description="When the slot starts, in UTC.")
    end_time: datetime = Field(description="When the slot ends, in UTC.")


class OpenSlotListResponse(BaseModel):
    """One page of open slots in the shared pagination envelope. total
    counts every open slot matching the filters, not just this page."""

    items: list[OpenSlotResponse]
    total: int
    limit: int
    offset: int

"""ProviderSchedule request/response schemas, plus the slot-generation
request/response.
"""

from datetime import date, time

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderScheduleCreate(BaseModel):
    """provider_id is not here -- it comes from the URL path
    (/providers/{provider_id}/schedules), so there is no way for the path
    and the body to disagree about which provider this is for.
    """

    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    slot_duration_minutes: int = Field(default=15, gt=0)

    @model_validator(mode="after")
    def check_end_after_start(self) -> "ProviderScheduleCreate":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class ProviderScheduleUpdate(BaseModel):
    """No cross-field check here the way Create has one: a PATCH might
    touch only end_time, and this schema has no way to know the existing
    start_time to compare against. That comparison is the database's
    end_after_start CHECK constraint's job -- see update_provider_schedule.
    """

    start_time: time | None = None
    end_time: time | None = None
    slot_duration_minutes: int | None = Field(default=None, gt=0)
    is_active: bool | None = None


class ProviderScheduleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_id: int
    weekday: int
    start_time: time
    end_time: time
    slot_duration_minutes: int
    is_active: bool


class ProviderScheduleListResponse(BaseModel):
    items: list[ProviderScheduleResponse]
    total: int
    limit: int
    offset: int


class GenerateSlotsRequest(BaseModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def check_range(self) -> "GenerateSlotsRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class GenerateSlotsResponse(BaseModel):
    created: int
    skipped: int

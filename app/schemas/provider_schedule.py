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
        """Reject a window that ends before it starts, or is zero-length.

        mode="after" so both fields are already parsed into `time` objects
        and can be compared. The database enforces this too, via the
        end_after_start CHECK -- this just turns it into a clean 422
        instead of a 500 from an IntegrityError.
        """
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
    """One recurring schedule window as returned to a client.

    start_time/end_time are clinic-local times of day with no date and no
    timezone -- they are intent, not instants. The UTC instants live on
    the Slot rows generate_slots produces from this.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_id: int
    weekday: int
    start_time: time
    end_time: time
    slot_duration_minutes: int
    is_active: bool


class ProviderScheduleListResponse(BaseModel):
    """One page of a single provider's schedule windows, in the shared
    pagination envelope. total counts that provider's windows only."""

    items: list[ProviderScheduleResponse]
    total: int
    limit: int
    offset: int


class GenerateSlotsRequest(BaseModel):
    """The date range to generate slots over, inclusive of both ends and
    interpreted as clinic-local calendar days.

    start_date == end_date is allowed and means a single day.
    """

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def check_range(self) -> "GenerateSlotsRequest":
        """Reject a backwards range up front.

        Without this the generator's loop simply never runs and returns
        created=0, which looks like a successful no-op rather than the
        bad request it actually is.
        """
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class GenerateSlotsResponse(BaseModel):
    """What a generation run did.

    Both numbers matter: `skipped` is how the caller can tell "this range
    was already generated" apart from "these templates produce nothing",
    which both leave created=0.
    """

    created: int
    skipped: int

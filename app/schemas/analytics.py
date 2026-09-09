"""Analytics schemas: the shared date range, and the two response shapes."""

from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, model_validator

# 30 days *including* today, so the default is a month of history rather
# than a month plus today.
DEFAULT_RANGE_DAYS = 30

# A year and a leap day. The series returns one row per day and is
# deliberately not paginated, so something has to bound the response --
# a range cap does that without making a client reassemble a chart
# across pages.
MAX_RANGE_DAYS = 366


class DateRange(BaseModel):
    """The start_date / end_date query params, defaulted and validated.

    A model rather than two bare Query parameters so that "start before
    end" lives with the fields it constrains, and fails as a 422 through
    the same path as every other validation error. A router raising it by
    hand would be business logic leaking upward.
    """

    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _resolve_and_check(self) -> "DateRange":
        """Fill in the defaults, then reject a range that makes no sense."""
        if self.end_date is None:
            # Not date.today(): that reads the process's local timezone,
            # and analytics_daily buckets are UTC calendar days.
            self.end_date = datetime.now(UTC).date()
        if self.start_date is None:
            self.start_date = self.end_date - timedelta(days=DEFAULT_RANGE_DAYS - 1)

        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if (self.end_date - self.start_date).days + 1 > MAX_RANGE_DAYS:
            raise ValueError(f"date range must not exceed {MAX_RANGE_DAYS} days")
        return self


class DailyBucket(BaseModel):
    """One day of the appointments-booked series."""

    date: date
    appointments_booked: int


class AppointmentsSeriesResponse(BaseModel):
    """Appointments booked per day across the requested range.

    The range is echoed back because both bounds may have been defaulted
    by the server -- a client that sent neither would otherwise have no
    idea which days it is looking at.
    """

    start_date: date
    end_date: date
    buckets: list[DailyBucket]


class AnalyticsSummaryResponse(BaseModel):
    """The six metrics for one date range.

    cancellation_rate and avg_wait_seconds are null rather than 0 when
    there is nothing to divide by. Zero and "no data" are different
    facts: a day with no bookings did not have a 0% cancellation rate.
    Same distinction the old avg_wait_seconds column was nullable for.
    """

    start_date: date
    end_date: date

    # A running total, not a per-day bucket, so it ignores the range.
    total_patients: int

    appointments_booked: int
    completed_visits: int
    cancellations: int
    cancellation_rate: float | None
    avg_wait_seconds: float | None
    failed_jobs: int


class FieldDrift(BaseModel):
    """One column where the aggregate and the raw tables disagree."""

    stored: float
    actual: float


class DayDrift(BaseModel):
    """One day, and every field of it that disagrees."""

    date: date
    fields: dict[str, FieldDrift]


class ReconciliationResponse(BaseModel):
    """The drift report.

    days_checked is here so an empty drifted_days list can be read
    correctly. "Nothing drifted" and "nothing was checked" look identical
    otherwise, and only one of them is good news.
    """

    start_date: date
    end_date: date
    days_checked: int
    in_sync: bool
    drifted_days: list[DayDrift]

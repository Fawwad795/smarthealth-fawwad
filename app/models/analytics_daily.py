"""AnalyticsDaily: one pre-aggregated row per calendar day.

Read by the (future) analytics endpoints instead of them ever running
COUNT(*)/AVG() over the raw tables directly. Written by the Week 3 Celery
rollup (app/services/analytics.py) today, and will also be written
incrementally by the Kafka consumer once task 3.4 exists -- 3.7's
reconciliation check is what proves the two never disagree.
"""

from datetime import date

from sqlalchemy import Date, Float, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class AnalyticsDaily(Base, TimestampMixin):
    """One day's worth of the brief's per-day metrics.

    date is the primary key rather than a separate surrogate id -- nothing
    else in the schema references this table by foreign key, and a day is
    already a unique, natural identity on its own. updated_at (from
    TimestampMixin) shows when this row was last recomputed.

    failed_jobs_count is deliberately absent too, for a different reason.
    No event announces a failed job -- failed_jobs rows are written by
    Celery's DeadLetterTask and by the consumer's own dead-letter path,
    neither of which publishes anything -- so nothing this consumer
    receives could ever maintain it. And it would be a second copy of a
    number failed_jobs already holds: that table gets a row only when
    something breaks, so counting it directly stays cheap, and one source
    of truth cannot disagree with itself.
    """

    __tablename__ = "analytics_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)

    appointments_booked: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    completed_visits: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    cancellations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    # An average cannot be incremented -- "8.5 minutes" has forgotten how
    # many visits produced it, so the next wait cannot be folded in. The
    # two components each can be, and the endpoint divides. Storing the
    # parts rather than the answer is what makes this column maintainable
    # one event at a time.
    wait_seconds_total: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0"
    )

    # Not the same as completed_visits: a visit contributes to the wait
    # only if it was checked in, and it is bucketed by the day it was
    # checked in rather than the day it completed. Dividing by the wrong
    # count is the easiest way to produce a plausible, wrong average.
    wait_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

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

    Total patients, the sixth metric in the brief, is deliberately absent:
    it's a single running count, not a per-day bucket, so it doesn't belong
    in a table shaped like this one.
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

    # Null, not 0, when nobody checked in that day -- 0 seconds average wait
    # and "no data" are different facts and must stay distinguishable.
    avg_wait_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    failed_jobs_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

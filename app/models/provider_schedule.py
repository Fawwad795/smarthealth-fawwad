"""ProviderSchedule: a provider's recurring weekly working hours."""

from datetime import time
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    SmallInteger,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.provider import Provider


class ProviderSchedule(Base, TimestampMixin):
    """Recurring intent, in clinic-local time: "Mondays, 09:00 to 17:00".

    Not availability. Nothing is bookable until the generator turns a
    schedule into concrete Slot rows. There is deliberately no foreign key
    from slots back to here: once generated a slot stands alone, so editing
    a template can never retroactively move or delete a slot someone has
    already booked.

    Overlapping windows for one provider on one weekday are not prevented
    here -- Postgres ships no range type for `time`, and creating a custom
    one would be exactly the hand-maintained database object we avoided with
    native enums. The consequence is caught where it matters: overlapping
    windows can only cause harm by producing overlapping slots, and that
    insert is rejected by the exclusion constraint on slots.
    """

    __tablename__ = "provider_schedules"
    __table_args__ = (
        # The same window entered twice for one provider. Not full overlap
        # protection -- see the class docstring.
        UniqueConstraint("provider_id", "weekday", "start_time"),
        CheckConstraint("end_time > start_time", name="end_after_start"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("slot_duration_minutes > 0", name="slot_duration_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    provider_id: Mapped[int] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # 0 = Monday .. 6 = Sunday, matching Python's date.weekday(). Chosen over
    # the Postgres EXTRACT(DOW) convention (0 = Sunday) because the generator
    # is Python walking dates -- an off-by-one here would produce slots on the
    # wrong days with no error at all.
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    # Time of day with no date and no timezone, and that is correct: "09:00
    # every Monday" is not an instant. It becomes one only when combined with
    # a date and Clinic.timezone, which the generator does -- and its output
    # is the UTC-aware slots.start_time.
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)

    slot_duration_minutes: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("15")
    )

    # Retired rather than deleted, so already-generated slots keep their
    # provenance and the row stays available for audit.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    provider: Mapped["Provider"] = relationship(back_populates="schedules")

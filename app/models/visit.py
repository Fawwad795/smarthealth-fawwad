"""Visit: what actually happened on the day of a confirmed appointment."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import VisitStatus, enum_column
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment


class Visit(Base, TimestampMixin):
    """One confirmed appointment's check-in-to-completion record.

    Created at check-in and never before, so "no row" is exactly what
    "the patient hasn't arrived" means -- which is also why the check-in
    service returns an existing row rather than erroring when one is
    already there.

    unique=True on appointment_id is the whole 1:1. Without it an
    appointment could accumulate several visits and "was this patient
    checked in?" would stop having a single answer.
    """

    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    status: Mapped[VisitStatus] = mapped_column(
        enum_column(VisitStatus, "status"),
        nullable=False,
        index=True,
        server_default=VisitStatus.CHECKED_IN.value,
    )

    # NOT NULL with no default: the row cannot exist without a check-in,
    # because check-in is what creates it. Week 3's average-wait-time
    # metric is this minus the slot's scheduled start.
    checked_in_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Null until COMPLETED. Together with checked_in_at and status these
    # three columns are the visit's whole audit trail -- a linear flow
    # with no branching or compensation needs no separate history table,
    # unlike appointment_status_history.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    appointment: Mapped["Appointment"] = relationship(back_populates="visit")

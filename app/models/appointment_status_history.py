"""AppointmentStatusHistory: an append-only log of every status change an
Appointment goes through.

Never updated and never deleted after being written -- each row is one
transition, in order, and together they are the only place the *sequence*
of a booking's states survives once Appointment.status has moved on.
"""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import AppointmentStatus, enum_column
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment


class AppointmentStatusHistory(Base, TimestampMixin):
    """One row per transition. created_at (from TimestampMixin) is when it
    happened -- there is no separate "changed_at" column.
    """

    __tablename__ = "appointment_status_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Null only on the row logging an appointment's creation -- there is no
    # "from" state before REQUESTED.
    from_status: Mapped[AppointmentStatus | None] = mapped_column(
        enum_column(AppointmentStatus, "from_status"), nullable=True
    )
    to_status: Mapped[AppointmentStatus] = mapped_column(
        enum_column(AppointmentStatus, "to_status"), nullable=False
    )

    # Who or what caused the transition -- "PATIENT", "FRONT_DESK",
    # "TEMPORAL_WORKFLOW" and so on. A plain string rather than a FK to
    # users: not every actor is a user (the saga itself causes transitions),
    # and this is an operational label, not an identity to authorize against.
    actor: Mapped[str] = mapped_column(String(30), nullable=False)

    # Operational only -- e.g. "billing pre-check failed". Never patient text.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    appointment: Mapped["Appointment"] = relationship(back_populates="status_history")

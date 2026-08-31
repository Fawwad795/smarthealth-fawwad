"""SlotReservation: an idempotency and compensation record for one slot hold.

Exists so the reserve_slot Activity (task 2.9) can tell a genuine retry
apart from a fresh attempt: the atomic UPDATE on slots is not by itself
enough, because a retried Activity call sees the slot already RESERVED
and cannot tell whether *it* reserved it moments ago or someone else beat
it there first. This row answers that question, and it's what
compensation (release_slot) flips back when a booking fails downstream.
"""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import SlotReservationStatus, enum_column
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment


class SlotReservation(Base, TimestampMixin):
    """One row per slot-hold attempt for one appointment.

    Not unique on appointment_id alone: a future reschedule (task 2.10)
    reserves a second slot for the same appointment while this row for
    the first stays as history, marked RELEASED.
    """

    __tablename__ = "slot_reservations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    slot_id: Mapped[int] = mapped_column(
        ForeignKey("slots.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[SlotReservationStatus] = mapped_column(
        enum_column(SlotReservationStatus, 'status'),
        nullable=False,
        server_default=SlotReservationStatus.RESERVED.value,
    )

    appointment: Mapped["Appointment"] = relationship(back_populates="slot_reservations")

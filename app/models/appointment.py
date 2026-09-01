"""Appointment: one booking attempt from request through to its outcome.

Never deleted, only transitioned -- see appointment_status_history.py for
the audit trail that makes that guarantee meaningful. No booking logic
lives here yet; that's the Week 2 scheduling saga (task 2.9), which reads
and writes this row through its Activities.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import AppointmentStatus, enum_column
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.appointment_status_history import AppointmentStatusHistory
    from app.models.billing import Billing
    from app.models.patient import Patient
    from app.models.provider import Provider
    from app.models.service import Service
    from app.models.slot import Slot
    from app.models.slot_reservation import SlotReservation
    from app.models.visit import Visit


class Appointment(Base, TimestampMixin):
    """One patient's request to see one provider, for one service, in one slot.

    idempotency_key is unique: it's the client's Idempotency-Key header
    (task 2.7), persisted so a repeated request is a database-provable
    duplicate even independently of the Redis-based fast path.
    """

    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slot_id: Mapped[int] = mapped_column(
        ForeignKey("slots.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    status: Mapped[AppointmentStatus] = mapped_column(
        enum_column(AppointmentStatus, "status"),
        nullable=False,
        index=True,
        server_default=AppointmentStatus.REQUESTED.value,
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )

    # Null until status reaches CONFIRMED. Distinct from created_at, which
    # marks when the REQUESTED row was written, not when the booking
    # actually succeeded.
    booked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    patient: Mapped["Patient"] = relationship(back_populates="appointments")
    provider: Mapped["Provider"] = relationship(back_populates="appointments")
    slot: Mapped["Slot"] = relationship(back_populates="appointments")
    service: Mapped["Service"] = relationship(back_populates="appointments")
    status_history: Mapped[list["AppointmentStatusHistory"]] = relationship(
        back_populates="appointment", order_by="AppointmentStatusHistory.created_at"
    )
    billing: Mapped["Billing | None"] = relationship(back_populates="appointment")
    slot_reservations: Mapped[list["SlotReservation"]] = relationship(
        back_populates="appointment"
    )
    visit: Mapped["Visit | None"] = relationship(back_populates="appointment")

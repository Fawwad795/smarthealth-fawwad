"""Billing: a simulated pre-check result for one appointment.

Real payment/insurance is explicitly out of scope (CLAUDE.md #4). This
table exists so the Week 2 scheduling saga (task 2.9) has something
concrete to succeed or fail against -- its compensation path (releasing a
slot after a billing failure) needs a real trigger, not a stub that
always returns True.
"""

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import BillingStatus, enum_column
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment


class Billing(Base, TimestampMixin):
    """One pre-check result for one appointment.

    appointment_id is unique: precheck() is meant to run exactly once per
    appointment, and a retried Activity call must find the row already
    written by the first attempt rather than create a second one. This
    constraint is the database-level backstop for that, the same role
    Appointment.idempotency_key's uniqueness plays for bookings.
    """

    __tablename__ = "billing"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    # Billing is simulated -- there is no pricing model anywhere in the
    # domain (Service and ProviderService carry no price field). A fixed
    # placeholder amount is enough to exercise CHECKED/FAILED and the
    # saga's compensation path, which is the entire point of this table
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    status: Mapped[BillingStatus] = mapped_column(
        enum_column(BillingStatus, "status"),
        nullable=False,
        index=True,
        server_default=BillingStatus.PENDING.value,
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )

    appointment: Mapped["Appointment"] = relationship(back_populates="billing")

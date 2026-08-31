"""Waitlist: one patient's standing request for any opening with one provider."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import WaitlistStatus, enum_column
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.patient import Patient
    from app.models.provider import Provider


class Waitlist(Base, TimestampMixin):
    """A place in the queue for a provider's time.

    Points at a provider, not a slot. Someone joining a waitlist does not
    know which slot will free up, only whose time they are waiting for --
    a per-slot queue would mean joining one queue per slot in the calendar,
    and every one of those becomes meaningless the moment that slot is
    booked.

        Ordering is by created_at (from TimestampMixin) rather than a position
    column: a position has to be renumbered every time someone leaves the
    queue, and two concurrent joins can be handed the same number. The
    arrival time never needs rewriting -- and because Postgres now() is
    transaction-scoped, two rows written in one transaction share it, so
    "next in line" orders by (created_at, id) with id as the tiebreak.
    """

    __tablename__ = "waitlist"
    __table_args__ = (
        # One live place per patient per provider. Partial, so it constrains
        # only WAITING rows: once an entry moves to OFFERED it stops blocking
        # a fresh join, and the history of past entries stays intact. The
        # service checks this before inserting to return a clean 409; this
        # index is the backstop for two concurrent joins that both pass that
        # check -- the same check-plus-constraint pairing appointments uses
        # for idempotency_key.
        Index(
            "uq_waitlist_one_waiting_entry",
            "provider_id",
            "patient_id",
            unique=True,
            postgresql_where=text("status = 'WAITING'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    provider_id: Mapped[int] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    status: Mapped[WaitlistStatus] = mapped_column(
        enum_column(WaitlistStatus, "status"),
        nullable=False,
        index=True,
        server_default=WaitlistStatus.WAITING.value,
    )

    provider: Mapped["Provider"] = relationship(back_populates="waitlist_entries")
    patient: Mapped["Patient"] = relationship(back_populates="waitlist_entries")

"""Slot: one bounded interval of one provider's time."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    column,
    func,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import SlotStatus, enum_column
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.provider import Provider


class Slot(Base, TimestampMixin):
    """A discrete, bookable interval of provider time.

    Not a boolean is_available flag on the provider. A boolean has no
    identity, so no appointment could point at it, no history could reference
    it, and two writers could flip it with nothing to conflict over. A row has
    a primary key, a status the database can arbitrate, and a lifetime that
    can be audited.

    Not service-specific either: this is provider time. The service is chosen
    when the appointment is created.

    The status column is what the Week 2 reservation guards on:

        UPDATE slots SET status = 'RESERVED', updated_at = now()
        WHERE id = :slot_id AND status = 'AVAILABLE'
        RETURNING id;
    """

    __tablename__ = "slots"
    __table_args__ = (
        # Not cosmetic. In Postgres an EMPTY range overlaps nothing -- not
        # even an identical empty range -- so a zero-length slot would slip
        # straight past the exclusion constraint below. This check is what
        # guarantees every range is non-empty, and therefore what makes the
        # overlap guarantee apply to every row.
        CheckConstraint("end_time > start_time", name="end_after_start"),
        # Redundant with the exclusion constraint for correctness -- identical
        # ranges overlap, so a duplicate would be rejected anyway. Kept for
        # its B-tree index, which serves the most frequent read in the system:
        #     WHERE provider_id = :p AND start_time >= :from AND start_time < :to
        #     ORDER BY start_time
        # The exclusion constraint's GiST index answers overlap questions well
        # and ordered range scans poorly. It also gives a far clearer error
        # when a day's slots are generated twice.
        UniqueConstraint("provider_id", "start_time"),
        # The guarantee no amount of application code can provide. Checking
        # for overlap in the generator is the same check-then-act gap as
        # SELECT-then-UPDATE: two concurrent runs both look, both find
        # nothing, both insert. Postgres evaluates this as part of the insert,
        # so there is no window.
        #
        # Requires the btree_gist extension (enabled by migration): the GiST
        # index type behind exclusion constraints does not handle plain
        # equality on an integer without it.
        ExcludeConstraint(
            ("provider_id", "="),
            (func.tstzrange(column("start_time"), column("end_time")), "&&"),
            name="ex_slots_no_overlap",
            using="gist",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # No index= here: the composite unique above already indexes
    # (provider_id, start_time), and a B-tree serves queries on its leftmost
    # column, so a separate single-column index would be pure write cost.
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=False,
    )

    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Indexed because availability queries always filter on it. Selectivity
    # will be poor once most slots are AVAILABLE; if that shows up in a real
    # query plan in Week 2, a composite (provider_id, status, start_time) is
    # the likely answer.
    status: Mapped[SlotStatus] = mapped_column(
        enum_column(SlotStatus, "status"),
        nullable=False,
        index=True,
        server_default=SlotStatus.AVAILABLE.value,
    )

    provider: Mapped["Provider"] = relationship(back_populates="slots")

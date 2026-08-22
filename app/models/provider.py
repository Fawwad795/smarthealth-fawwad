"""Provider: the operational profile of a clinician."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.department import Department
    from app.models.provider_schedule import ProviderSchedule   
    from app.models.slot import Slot  
    from app.models.specialty import Specialty
    from app.models.user import User


class Provider(Base, TimestampMixin):
    """Who a clinician is, where they sit, and what they specialise in.

    Not a schedule and not availability: provider time lives entirely in
    provider_schedules (the recurring intent) and slots (the concrete,
    bookable reality).
    """

    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    # NOT NULL is only possible because this table holds providers and nothing
    # else. On a single merged users table it would have to be nullable,
    # because patients have no department -- and "every provider belongs to a
    # department" would be a convention rather than a rule.
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    specialty_id: Mapped[int] = mapped_column(
        ForeignKey("specialties.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Nullable for the same reason as Service.description: a provider must be
    # creatable before someone writes their biography. It feeds the Week 4
    # chunk text, and completeness for that purpose is checked where that
    # purpose lives, not by the database.
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="provider")
    department: Mapped["Department"] = relationship(back_populates="providers")
    specialty: Mapped["Specialty"] = relationship(back_populates="providers")
    schedules: Mapped[list["ProviderSchedule"]] = relationship(back_populates="provider")
    slots: Mapped[list["Slot"]] = relationship(back_populates="provider")
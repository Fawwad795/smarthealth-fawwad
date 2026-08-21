"""Department: an organisational grouping inside a clinic."""

from typing import TYPE_CHECKING

from sqlalchemy import (BigInteger, ForeignKey, Integer, String, UniqueConstraint, text,)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

# Only imported for type checkers and editors. Not executed at runtime, so
# these do not create an import cycle -- SQLAlchemy resolves "Clinic" and
# "Service" from its own class registry once every model has loaded.
if TYPE_CHECKING:
    from app.models.clinic import Clinic
    from app.models.service import Service

class Department(Base, TimestampMixin):
    """A grouping inside a clinic that owns both providers and services.

    Not a specialty: a department groups people and offerings ("Orthopaedics"),
    while a specialty describes one provider's expertise. Keeping them separate
    lets a department hold providers of several specialties.
    """

    __tablename__ = "departments"
    __table_args__ = (
        # Two departments in one clinic may not share a name. Names are how a
        # patient is told where to go, so a duplicate is an ambiguity rather
        # than a harmless repeat.
        UniqueConstraint("clinic_id", "name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="RESTRICT"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Display ordering only. Unlike Clinic.timezone a wrong value here is
    # cosmetic, so a default costs nothing and saves every insert from
    # supplying it.
    order_index: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    clinic: Mapped["Clinic"] = relationship(back_populates="departments")
    services: Mapped[list["Service"]] = relationship(back_populates="department")

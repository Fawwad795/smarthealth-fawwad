"""The clinic: the physical site that owns departments."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.department import Department


class Clinic(Base, TimestampMixin):
    """A physical site.

    Not a tenant boundary -- multi-clinic sync is out of scope and no query is
    scoped by clinic. It is a table rather than a string on departments so the
    name is stored once, and because it carries the timezone: the one place
    that knows how to turn a provider's local working hours into UTC slot
    boundaries in the slot generator.
    """

    __tablename__ = "clinics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)

    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    departments: Mapped[list["Department"]] = relationship(back_populates="clinic")




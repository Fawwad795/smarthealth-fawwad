"""Specialty: the controlled vocabulary of provider expertise."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.provider import Provider


class Specialty(Base, TimestampMixin):
    """What a provider specialises in.

    A table rather than a free-text column on providers because Week 4 filters
    retrieval on specialty as vector metadata, and "Cardiology" / "cardiology"
    / "Cardiolgy" as three distinct strings would silently break that filter
    rather than fail loudly.

    Describes a provider, never a patient. Nothing here is clinical.
    """

    __tablename__ = "specialties"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    providers: Mapped[list["Provider"]] = relationship(back_populates="specialty")


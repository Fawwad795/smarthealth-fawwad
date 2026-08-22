"""Patient: the operational profile of someone who receives care."""

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Date, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class Patient(Base, TimestampMixin):
    """Operational profile only -- never a medical record.

    No diagnoses, prescriptions, lab results or clinical notes exist anywhere
    in this system. This row holds what is needed to schedule and contact
    someone, and nothing else.
    """

    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # unique=True is the entire one-to-one. Without it a user could have two
    # patient rows, and "the patient row for this user" -- the basis of every
    # PHI-scoped query -- would no longer identify a single row.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    # A calendar date, deliberately not a timestamp: a birthday has no
    # timezone, and storing it as TIMESTAMPTZ would introduce a conversion
    # that can shift it across midnight into the previous day.
    dob: Mapped[date] = mapped_column(Date, nullable=False)

    # PHI. Lives in this column and nowhere else -- never in logs, events or
    # ai_interactions, which carry patient_id instead.
    #
    # JSONB because contact details are a shifting bag (phone, alternate
    # phone, preferred channel) and the brief rules out a second database.
    # Postgres will accept any JSON here, so the shape is validated by a
    # Pydantic model at the schema layer; without that it becomes a junk
    # drawer of three spellings of "phone".
    contact: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    user: Mapped["User"] = relationship(back_populates="patient")

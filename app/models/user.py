"""User: the identity that logs in."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import UserRole, enum_column
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.patient import Patient
    from app.models.provider import Provider

class User(Base, TimestampMixin):
    """An account. Identity only -- what a person *is* lives on their profile.

    Never serialised to a client: this row holds password_hash, and returning
    an ORM object straight from an endpoint is how that leaks. Responses are
    built from Pydantic schemas that name their fields explicitly.
    """

    __tablename__ = "users"
    __table_args__ = (
        # Uniqueness on the LOWERCASED address. Postgres compares text
        # byte-for-byte, so a plain unique constraint would let
        # fawwad@x.com and Fawwad@x.com both exist -- two accounts for one
        # person, each with its own patient row and its own appointments.
        # Enforced in the database rather than only in the service layer so
        # that a seed script or a psql session cannot bypass it.
        #
        # Consequence: every lookup must be written
        #     WHERE lower(email) = :email
        # or Postgres cannot use this index.
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    email: Mapped[str] = mapped_column(String(255), nullable=False)

    # Named "hash", never "password": the column name is the cheapest defence
    # against someone putting a plaintext value here. 255 covers bcrypt (60)
    # and argon2 (~100) so changing algorithm needs no migration.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole, "role"), nullable=False
    )

    # Accounts are deactivated, never deleted -- same principle as RESTRICT on
    # the foreign keys and as transitioning rather than deleting appointments.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    # Optional on both sides: an admin or front_desk user has neither profile.
    # A single object rather than a list is what makes these one-to-one -- the
    # UNIQUE on the profile's user_id is what makes that true in the database.
    patient: Mapped["Patient | None"] = relationship(back_populates="user")
    provider: Mapped["Provider | None"] = relationship(back_populates="user")
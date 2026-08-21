"""Service: an operational offering a patient can be pointed at."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ServiceStatus, enum_column
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.department import Department


class Service(Base, TimestampMixin):
    """Something the clinic offers: what it is, how to prepare, whether it is live.

    Logistics, never clinical content. `description` and `prep_instructions`
    say "arrive 15 minutes early, bring previous imaging" -- they never give
    medical advice.

    The lifecycle is a status enum rather than an is_published boolean because
    Week 2 must distinguish "a publish workflow is running right now" and "the
    last publish failed" from "not published".
    """

    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("department_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)

    # Nullable on purpose. A service is created as a DRAFT and filled in over
    # time; the publish workflow's first activity validates completeness and
    # returns every missing field at once. That check only has anything to
    # report if these columns are allowed to be empty before publishing.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    prep_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[ServiceStatus] = mapped_column(
        enum_column(ServiceStatus, "status"),
        nullable=False,
        index=True,
        server_default=ServiceStatus.DRAFT.value,
    )

    # Null until a publish succeeds. Week 4's retrieval filter reads this
    # alongside status == PUBLISHED: never recommend a service the clinic does
    # not currently offer.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    department: Mapped["Department"] = relationship(back_populates="services")

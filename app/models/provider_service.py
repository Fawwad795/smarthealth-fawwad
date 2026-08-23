"""ProviderService: which providers are qualified to deliver which services."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.provider import Provider
    from app.models.service import Service


class ProviderService(Base, TimestampMixin):
    """States a fact the schema otherwise has no way to express: that this
    provider is qualified to deliver this service.

    Not inferred from department_id. A provider and a service can share a
    department without the provider actually performing that service -- a
    department groups people and offerings, it does not certify who does
    what. Week 2's eligibility check and the search-by-service listing both
    need this real answer, not the department coincidence.
    """

    __tablename__ = "provider_services"
    __table_args__ = (UniqueConstraint("provider_id", "service_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # No index= here: the composite unique below already indexes
    # (provider_id, service_id), so a lookup filtered on provider_id alone
    # already has a leftmost-prefix match -- same reasoning as services.
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Indexed on its own: "who offers this service" filters on service_id
    # alone, which is not the leading column of the composite unique above.
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    provider: Mapped["Provider"] = relationship(back_populates="provider_services")
    service: Mapped["Service"] = relationship(back_populates="provider_services")

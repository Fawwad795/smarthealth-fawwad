"""Notification: the record that a patient or staff member was told something.

"Told" is deliberately minimal -- CLAUDE.md #4 rules out real SMS/email, so a
notification is this row plus a log line, nothing more.
"""

from typing import Any

from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import NotificationStatus, NotificationType, enum_column
from app.models.mixins import TimestampMixin


class Notification(Base, TimestampMixin):
    """One notification sent to one user.

    payload carries ids only (e.g. {"appointment_id": 812}), never names or
    contact details -- the same PHI discipline as event envelopes and logs
    (CLAUDE.md #6), even though this table itself is never published anywhere.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    type: Mapped[NotificationType] = mapped_column(
        enum_column(NotificationType, "type"),
        nullable=False,
    )

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    status: Mapped[NotificationStatus] = mapped_column(
        enum_column(NotificationStatus, "status"),
        nullable=False,
        server_default=NotificationStatus.SENT.value,
    )

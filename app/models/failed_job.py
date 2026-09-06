"""FailedJob: the dead-letter record for a background task that gave up.

Whatever asynchronous work exists in this project -- Celery tasks so far --
writes here on its last, unrecoverable failure, so "it failed and nobody
noticed" (one of the five problems this project exists to fix) has a
queryable answer instead of a line lost in a worker's log output.
"""

from typing import Any

from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class FailedJob(Base, TimestampMixin):
    """One task that retried until Celery gave up on it.

    created_at (from TimestampMixin) is "when it finally failed" -- no
    separate first-attempted-at column, since attempts is the count and
    individual retry timestamps aren't needed for anything this project
    does with the table.
    """

    __tablename__ = "failed_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # The registered Celery task name (e.g.
    # "app.workers.tasks.reminders.send_appointment_reminder"), not a free
    # description -- lets a later metrics endpoint group failures by task.
    job_type: Mapped[str] = mapped_column(
        String(255), 
        nullable=False, 
        index=True
    )

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    error: Mapped[str] = mapped_column(Text, nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False)

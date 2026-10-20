"""GeneratedContent: one staff generation and what it produced (task 5.10).

Saved with the prompt version and model that produced it, as the brief
asks, so a generation can be reviewed and run again under the same
conditions. Every run is saved, interrupted and failed ones included:
`content` then holds whatever text was streamed before it stopped.

The run's outcome, token counts and latency are on its ai_interactions
row, linked by interaction_id, rather than copied here.

`content` holds a summary or follow-up about one appointment: its service,
department, time and status, never the patient's name or contact details,
which the model is never sent. Only front desk and admin can generate it.
"""

from datetime import date
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import GenerationKind, enum_column
from app.models.mixins import TimestampMixin


class GeneratedContent(Base, TimestampMixin):
    """One summary, follow-up draft or utilisation report.

    Never updated once written: it records a generation that has finished.
    """

    __tablename__ = "generated_content"
    __table_args__ = (
        # Appendix B's "appointment_id/report_scope": an appointment's
        # summary or follow-up has the appointment and no dates; a report
        # has its date range and no appointment. Enforced here so no code
        # path can save a report that points at an appointment.
        # `report_end IS NOT NULL` is not implied by `report_end >=
        # report_start`: with a null end that comparison is null, and a
        # CHECK passes on null.
        CheckConstraint(
            "(kind IN ('SUMMARY', 'FOLLOWUP') AND appointment_id IS NOT NULL"
            " AND report_start IS NULL AND report_end IS NULL)"
            " OR (kind = 'UTILISATION_REPORT' AND appointment_id IS NULL"
            " AND report_start IS NOT NULL AND report_end IS NOT NULL"
            " AND report_end >= report_start)",
            name="scope",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    kind: Mapped[GenerationKind] = mapped_column(
        enum_column(GenerationKind, "kind"),
        nullable=False,
    )

    # The appointment a summary or follow-up is about. Indexed: staff look
    # up what has been generated for an appointment.
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # A report's date range, both days included, in UTC calendar days as
    # analytics_daily counts them.
    report_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    report_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    # {"text": "..."} for a summary or follow-up, and for any report that
    # did not pass validation. A validated report is stored as the report
    # itself: its figures, summary and observations.
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    model: Mapped[str] = mapped_column(String(100), nullable=False)

    # The version of the prompt that produced the content. A report that
    # needed its repair retry names REPORT_REPAIR_V1, which only ever
    # follows PROMPT_REPORT_V1, so the version alone says which happened.
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)

    # The staff member who generated it.
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # The run's ai_interactions row: outcome, tokens and latency. One row
    # each way, so unique.
    interaction_id: Mapped[int] = mapped_column(
        ForeignKey("ai_interactions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

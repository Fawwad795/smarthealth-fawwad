"""AIInteraction: one AI call made for a user, and how it went (task 5.7).

One row per question to the patient assistant, and per staff generation
(task 5.9). The AI analytics (task 5.11) are counted from these rows:
questions asked, answered against refused, the breakdown by intent, booking
conversion (a user's appointments booked after their interaction), latency
and tokens.

Ids, labels and numbers only, never text. The question is the patient's own
words and can hold anything, a symptom or another person's name, so it is
not stored. The answer is not stored either: the model can repeat the
question back, and a preparation answer holds the patient's appointment
details. What is stored is what the brief asks to reconstruct a wrong answer
from: which services and appointments the model was shown, which prompt
versions and which model. The correlation ID ties the row to the request's
log lines.
"""

from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AssistantIntent, InteractionOutcome, enum_column
from app.models.mixins import TimestampMixin


class AIInteraction(Base, TimestampMixin):
    """One question asked, or one generation run, and what came of it.

    Never updated once written: it records something that has finished.
    """

    __tablename__ = "ai_interactions"
    __table_args__ = (
        # Booking conversion (task 5.11) looks up one user's interactions and
        # compares their times with the appointments that user booked. With
        # user_id leading, this also serves any lookup by user alone.
        Index("ix_ai_interactions_user_id_created_at", "user_id", "created_at"),
        # The analytics count interactions over a date range, across users.
        Index("ix_ai_interactions_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # The caller: a patient asking, or a staff member generating. Indexed by
    # the composite index above.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # The request's correlation ID, so `grep` finds this interaction's log
    # lines. Null only outside a request.
    correlation_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    # What the safety check sorted the question into, or STAFF_GENERATION.
    # Null when the safety check gave no label, which is refused (task 5.4).
    intent: Mapped[AssistantIntent | None] = mapped_column(
        enum_column(AssistantIntent, "intent"),
        nullable=True,
    )

    outcome: Mapped[InteractionOutcome] = mapped_column(
        enum_column(InteractionOutcome, "outcome"),
        nullable=False,
    )

    # The ids of what the model was shown, by kind:
    # {"services": [3, 7], "appointments": [51], "slots": [273, 274]}.
    # Ids only, never text. JSONB, so adding a kind needs no migration.
    retrieved_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    # The model that answered. Null when no model was called, such as a
    # question refused before the model saw it.
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # The version of the safety prompt that sorted the question (null for
    # staff generation), and of the prompt or fixed reply that produced the
    # answer. Both are needed to reconstruct a wrong answer.
    safety_prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Summed over every model call the interaction made. A stream cut short
    # never gets its counts from the provider, so they cover the calls that
    # did report them.
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    # From the request arriving to the last of the answer leaving.
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    # How long the answer was, in characters: how much an INTERRUPTED answer
    # got through, without storing what it said.
    answer_chars: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    # True when the reply came from the answer cache (task 5.12): no model
    # was called for it, so its tokens are 0. `model` and the prompt
    # versions are those that wrote the reply when it was first answered.
    cache_hit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

"""Records each AI interaction in ai_interactions (task 5.7).

The one place a row is written, so POST /assistant/ask and staff generation
store the same fields the same way. It takes ids, labels and numbers, and
has no parameter that could carry the question or the answer, so no caller
can store either by mistake.

The correlation ID is read from the request's context here rather than
passed in, so a caller cannot forget it.
"""

import logging
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.ai.llm import LLMUsage
from app.core.logging import get_correlation_id
from app.models import AIInteraction
from app.models.enums import AssistantIntent, InteractionOutcome

logger = logging.getLogger(__name__)


def record_interaction(
    db: Session,
    *,
    user_id: int,
    outcome: InteractionOutcome,
    latency_ms: int,
    intent: AssistantIntent | None = None,
    service_ids: Iterable[int] = (),
    appointment_ids: Iterable[int] = (),
    slot_ids: Iterable[int] = (),
    model: str | None = None,
    safety_prompt_version: str | None = None,
    prompt_version: str | None = None,
    usages: Iterable[LLMUsage | None] = (),
    answer_chars: int = 0,
) -> AIInteraction:
    """Write one ai_interactions row, commit it, and log it.

    `slot_ids` are the free slots whose times an availability answer was
    shown, so the answer can be checked against them later.

    `usages` holds the token counts of each model call the interaction
    made, the safety check's and the answer's. None, from a stream cut
    short, adds nothing.

    Commits here rather than leaving it to the caller: the row records
    something that has already happened and is part of no larger change.
    An answer already sent cannot be rolled back with anything.
    """
    reported = [usage for usage in usages if usage is not None]
    interaction = AIInteraction(
        user_id=user_id,
        correlation_id=get_correlation_id(),
        intent=intent,
        outcome=outcome,
        retrieved_ids={
            "services": list(service_ids),
            "appointments": list(appointment_ids),
            "slots": list(slot_ids),
        },
        model=model,
        safety_prompt_version=safety_prompt_version,
        prompt_version=prompt_version,
        input_tokens=sum(usage.input_tokens for usage in reported),
        output_tokens=sum(usage.output_tokens for usage in reported),
        latency_ms=latency_ms,
        answer_chars=answer_chars,
    )
    db.add(interaction)
    db.commit()

    # The brief asks for every interaction in the logs too, with its
    # retrieved ids and token counts. The JSON formatter adds the
    # correlation ID to the line.
    logger.info(
        "ai interaction %d: outcome=%s intent=%s services=%s appointments=%s "
        "slots=%s model=%s prompt=%s tokens_in=%d tokens_out=%d latency_ms=%d",
        interaction.id,
        interaction.outcome,
        interaction.intent,
        interaction.retrieved_ids["services"],
        interaction.retrieved_ids["appointments"],
        interaction.retrieved_ids["slots"],
        interaction.model,
        interaction.prompt_version,
        interaction.input_tokens,
        interaction.output_tokens,
        interaction.latency_ms,
    )
    return interaction

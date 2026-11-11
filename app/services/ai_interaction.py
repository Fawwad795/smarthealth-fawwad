"""Records each AI interaction in ai_interactions (task 5.7).

The one place a row is written, so POST /assistant/ask and staff generation
store the same fields the same way. It takes ids, labels and numbers, and
has no parameter that could carry the question or the answer, so no caller
can store either by mistake.

The correlation ID is read from the request's context here rather than
passed in, so a caller cannot forget it.

Being the one place every interaction ends, it is also where the AI
metrics (task 5.11) are counted.
"""

import logging
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.ai.llm import LLMUsage
from app.core.logging import get_correlation_id
from app.core.metrics import (
    ai_interaction_duration,
    ai_interactions,
    ai_refusals,
    ai_tokens,
)
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
    cache_hit: bool = False,
) -> AIInteraction:
    """Write one ai_interactions row, commit it, and log it.

    `slot_ids` are the free slots whose times an availability answer was
    shown, so the answer can be checked against them later.

    `cache_hit` marks a reply served from the answer cache (task 5.12).

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
        cache_hit=cache_hit,
    )
    db.add(interaction)
    db.commit()

    # The brief asks for every interaction in the logs too, with its
    # retrieved ids and token counts. The JSON formatter adds the
    # correlation ID to the line.
    logger.info(
        "ai interaction %d: outcome=%s intent=%s services=%s appointments=%s "
        "slots=%s model=%s prompt=%s tokens_in=%d tokens_out=%d latency_ms=%d "
        "cache_hit=%s",
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
        interaction.cache_hit,
    )
    _count(interaction)
    return interaction


def _label(intent: AssistantIntent | None) -> str:
    """The metric label for an intent: "none" when the safety check gave no
    label, because a Prometheus label cannot be null."""
    return intent or "none"


def _count(interaction: AIInteraction) -> None:
    """Add one stored interaction to the Prometheus AI metrics.

    Called after the commit, beside the log line, so the metrics, the logs
    and the table all count the same interactions.
    """
    intent = _label(interaction.intent)
    ai_interactions.labels(intent, interaction.outcome).inc()
    # Prometheus measures time in seconds; the row stores milliseconds.
    ai_interaction_duration.labels(intent).observe(interaction.latency_ms / 1000)
    ai_tokens.labels("input").inc(interaction.input_tokens)
    ai_tokens.labels("output").inc(interaction.output_tokens)
    if interaction.outcome == InteractionOutcome.REFUSED:
        ai_refusals.labels(intent).inc()


def _start_metrics_at_zero() -> None:
    """Create every AI metric series at zero when the process starts.

    A labelled series otherwise first appears at its first increment,
    already at 1. Prometheus's increase() and rate() work from the change
    between two samples, so they would miss that first refusal: found when
    the runbook's "refusals in the last hour" query said 0 after one.
    """
    for intent in [*AssistantIntent, None]:
        ai_interaction_duration.labels(_label(intent))
        for outcome in InteractionOutcome:
            ai_interactions.labels(_label(intent), outcome)
    # Only these two are ever refused (SafetyCheck.refused).
    for intent in (AssistantIntent.MEDICAL_ADVICE, None):
        ai_refusals.labels(_label(intent))
    for direction in ("input", "output"):
        ai_tokens.labels(direction)


_start_metrics_at_zero()

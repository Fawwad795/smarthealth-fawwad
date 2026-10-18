"""record_interaction (task 5.7), against the migrated schema.

Each test reads the row back from the database rather than trusting the
object the function returned, so what is checked is what was stored.
"""

import logging

import pytest
from sqlalchemy.orm import Session

from app.ai.llm import LLMUsage
from app.core.logging import set_correlation_id
from app.models import AIInteraction, User
from app.models.enums import AssistantIntent, InteractionOutcome
from app.services.ai_interaction import record_interaction


def _stored(db: Session, interaction_id: int) -> AIInteraction:
    """The row as the database holds it, not as the session cached it."""
    db.expire_all()
    return db.get(AIInteraction, interaction_id)


def test_a_refusal_is_stored_with_its_ids_versions_and_correlation_id(
    db_session: Session, patient_user: User
) -> None:
    set_correlation_id("req-ai-refusal")

    row = record_interaction(
        db_session,
        user_id=patient_user.id,
        outcome=InteractionOutcome.REFUSED,
        latency_ms=412,
        intent=AssistantIntent.MEDICAL_ADVICE,
        service_ids=[3, 7],
        model="qwen/qwen3.8-27b",
        safety_prompt_version="PROMPT_SAFETY_V1",
        prompt_version="REFUSAL_V1",
        usages=[LLMUsage(input_tokens=180, output_tokens=2)],
        answer_chars=230,
    )

    stored = _stored(db_session, row.id)
    assert stored.user_id == patient_user.id
    assert stored.correlation_id == "req-ai-refusal"
    assert stored.intent == AssistantIntent.MEDICAL_ADVICE
    assert stored.outcome == InteractionOutcome.REFUSED
    assert stored.retrieved_ids == {"services": [3, 7], "appointments": []}
    assert stored.model == "qwen/qwen3.8-27b"
    assert stored.safety_prompt_version == "PROMPT_SAFETY_V1"
    assert stored.prompt_version == "REFUSAL_V1"
    assert (stored.input_tokens, stored.output_tokens) == (180, 2)
    assert (stored.latency_ms, stored.answer_chars) == (412, 230)


def test_token_counts_are_summed_and_a_call_that_reported_none_adds_nothing(
    db_session: Session, patient_user: User
) -> None:
    """The safety check reported its counts; the answer's stream was cut
    short, so the provider never sent them."""
    row = record_interaction(
        db_session,
        user_id=patient_user.id,
        outcome=InteractionOutcome.INTERRUPTED,
        latency_ms=950,
        intent=AssistantIntent.PREPARATION,
        appointment_ids=[51],
        usages=[LLMUsage(input_tokens=180, output_tokens=2), None],
        answer_chars=40,
    )

    stored = _stored(db_session, row.id)
    assert (stored.input_tokens, stored.output_tokens) == (180, 2)
    assert stored.retrieved_ids == {"services": [], "appointments": [51]}


def test_an_interaction_with_no_label_and_no_model_stores_empty_ids_and_zeros(
    db_session: Session, patient_user: User
) -> None:
    """What a refusal looks like when the safety check gave no label: no
    intent, and nothing retrieved or counted."""
    row = record_interaction(
        db_session,
        user_id=patient_user.id,
        outcome=InteractionOutcome.REFUSED,
        latency_ms=3,
        prompt_version="REFUSAL_NO_SERVICE_V1",
    )

    stored = _stored(db_session, row.id)
    assert stored.intent is None and stored.model is None
    assert stored.retrieved_ids == {"services": [], "appointments": []}
    assert (stored.input_tokens, stored.output_tokens) == (0, 0)
    assert stored.correlation_id is None


def test_the_log_line_carries_ids_and_counts_and_nothing_else(
    db_session: Session, patient_user: User, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.services.ai_interaction")

    row = record_interaction(
        db_session,
        user_id=patient_user.id,
        outcome=InteractionOutcome.ANSWERED,
        latency_ms=900,
        intent=AssistantIntent.NAVIGATION,
        service_ids=[5],
        model="qwen/qwen3.8-27b",
        prompt_version="PROMPT_NAV_V1",
        usages=[LLMUsage(input_tokens=10, output_tokens=20)],
        answer_chars=150,
    )

    (record,) = [r for r in caplog.records if r.name == "app.services.ai_interaction"]
    assert record.getMessage() == (
        f"ai interaction {row.id}: outcome=ANSWERED intent=NAVIGATION "
        "services=[5] appointments=[] model=qwen/qwen3.8-27b prompt=PROMPT_NAV_V1 "
        "tokens_in=10 tokens_out=20 latency_ms=900"
    )

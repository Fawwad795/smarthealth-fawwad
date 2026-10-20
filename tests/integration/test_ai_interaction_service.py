"""record_interaction (task 5.7), against the migrated schema.

Each test reads the row back from the database rather than trusting the
object the function returned, so what is checked is what was stored.
"""

import logging

import pytest
from prometheus_client import REGISTRY
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


def _sample(name: str, **labels: str) -> float:
    """One metric's current value, 0 if never set.

    Metrics are shared by every test in the run, so tests compare before
    and after rather than expect a fixed number.
    """
    return REGISTRY.get_sample_value(name, labels) or 0.0


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
    assert stored.retrieved_ids == {"services": [3, 7], "appointments": [], "slots": []}
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
        slot_ids=[201, 202],
        usages=[LLMUsage(input_tokens=180, output_tokens=2), None],
        answer_chars=40,
    )

    stored = _stored(db_session, row.id)
    assert (stored.input_tokens, stored.output_tokens) == (180, 2)
    assert stored.retrieved_ids == {
        "services": [],
        "appointments": [51],
        "slots": [201, 202],
    }


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
    assert stored.retrieved_ids == {"services": [], "appointments": [], "slots": []}
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
        "services=[5] appointments=[] slots=[] model=qwen/qwen3.8-27b prompt=PROMPT_NAV_V1 "
        "tokens_in=10 tokens_out=20 latency_ms=900"
    )


# --- Prometheus metrics (task 5.11) -------------------------------------------


def test_an_answer_is_counted_with_its_latency_in_seconds_and_its_tokens(
    db_session: Session, patient_user: User
) -> None:
    """An answer moves the interaction count, the latency and the tokens,
    and leaves the refusal counter alone."""
    labels = {"intent": "NAVIGATION", "outcome": "ANSWERED"}
    before = {
        "count": _sample("ai_interactions_total", **labels),
        "seconds": _sample("ai_interaction_duration_seconds_sum", intent="NAVIGATION"),
        "input": _sample("ai_tokens_total", direction="input"),
        "output": _sample("ai_tokens_total", direction="output"),
        "refused": _sample("ai_refusals_total", intent="NAVIGATION"),
    }

    record_interaction(
        db_session,
        user_id=patient_user.id,
        outcome=InteractionOutcome.ANSWERED,
        latency_ms=1500,
        intent=AssistantIntent.NAVIGATION,
        usages=[LLMUsage(input_tokens=10, output_tokens=20)],
    )

    assert _sample("ai_interactions_total", **labels) - before["count"] == 1
    assert _sample("ai_interaction_duration_seconds_sum", intent="NAVIGATION") - before[
        "seconds"
    ] == pytest.approx(1.5)
    assert _sample("ai_tokens_total", direction="input") - before["input"] == 10
    assert _sample("ai_tokens_total", direction="output") - before["output"] == 20
    assert _sample("ai_refusals_total", intent="NAVIGATION") == before["refused"]


def test_a_refusal_is_counted_and_one_without_a_label_counts_as_none(
    db_session: Session, patient_user: User
) -> None:
    """Both kinds of refusal reach the refusal counter. The one with no
    label is counted under "none", since a label cannot be null."""
    before = {
        "medical": _sample("ai_refusals_total", intent="MEDICAL_ADVICE"),
        "none": _sample("ai_refusals_total", intent="none"),
        "none_refused": _sample(
            "ai_interactions_total", intent="none", outcome="REFUSED"
        ),
    }

    for intent in (AssistantIntent.MEDICAL_ADVICE, None):
        record_interaction(
            db_session,
            user_id=patient_user.id,
            outcome=InteractionOutcome.REFUSED,
            latency_ms=5,
            intent=intent,
        )

    assert (
        _sample("ai_refusals_total", intent="MEDICAL_ADVICE") - before["medical"] == 1
    )
    assert _sample("ai_refusals_total", intent="none") - before["none"] == 1
    assert (
        _sample("ai_interactions_total", intent="none", outcome="REFUSED")
        - before["none_refused"]
        == 1
    )

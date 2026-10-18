"""The shape of ai_interactions, read from the model. No database.

The table must never hold the patient's question or the assistant's answer
(docs/ai-layer.md section 11). These tests make a new column a deliberate
change: one added to store either would have to be added here too, by
someone who has read why it is missing.
"""

from sqlalchemy import String, Text

from app.models import AIInteraction


def test_ai_interactions_has_exactly_these_columns() -> None:
    assert [column.name for column in AIInteraction.__table__.columns] == [
        "id",
        "user_id",
        "correlation_id",
        "intent",
        "outcome",
        "retrieved_ids",
        "model",
        "safety_prompt_version",
        "prompt_version",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "answer_chars",
        "created_at",
        "updated_at",
    ]


def test_no_column_is_long_enough_to_hold_free_text() -> None:
    """The longest string column is the model name. TEXT, or a long
    VARCHAR, is where a question or an answer would end up."""
    for column in AIInteraction.__table__.columns:
        if isinstance(column.type, String):
            assert not isinstance(column.type, Text), column.name
            assert column.type.length is not None, column.name
            assert column.type.length <= 100, column.name

"""The checks on a question sent to POST /assistant/ask (task 5.6).

They run in the request schema, before any AI work, so a question that
can never get a useful answer costs no embedding and no model call. The
route's 422 tests arrive with the route (task 5.3); these pin the rules.
"""

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.assistant import AskRequest


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": "x" * (settings.ai_max_question_chars + 1)},
    ],
    ids=["missing", "empty", "spaces", "one-over-the-limit"],
)
def test_an_empty_missing_or_oversized_question_is_rejected(body: dict) -> None:
    with pytest.raises(ValidationError):
        AskRequest.model_validate(body)


@pytest.mark.parametrize(
    "question",
    ["!!!!", "12345", "?? 42 !!", "  ...  "],
    ids=["punctuation", "digits", "mixed", "dots-and-spaces"],
)
def test_a_question_with_no_letters_is_rejected(question: str) -> None:
    with pytest.raises(ValidationError, match="at least one letter"):
        AskRequest.model_validate({"question": question})


@pytest.mark.parametrize(
    "question",
    [
        "MRI",
        "Do you offer knee scans?",
        "¿Ofrecen radiografías de rodilla?",
        "x" * settings.ai_max_question_chars,
    ],
    ids=["one-word", "sentence", "non-english-letters", "exactly-the-limit"],
)
def test_a_question_with_a_letter_is_accepted(question: str) -> None:
    assert AskRequest.model_validate({"question": question}).question == question


def test_word_shaped_gibberish_is_accepted_for_the_threshold_to_catch() -> None:
    # No rule here tells "asdkjh" from a real word it has never seen, such
    # as a service's name. Retrieval finds nothing above the threshold
    # instead, and the patient is told the clinic does not offer it.
    question = "asdkjh qwe zzz"

    assert AskRequest.model_validate({"question": question}).question == question


def test_the_question_is_stripped_before_it_is_checked() -> None:
    request = AskRequest.model_validate({"question": "  knee scan \n"})

    assert request.question == "knee scan"

"""The safety check and the refusal (task 5.4).

A question is refused when the safety check labels it medical_advice, or
when the reply names no label at all: a model that could not sort the
question has not shown it is safe to answer. The route's "diagnose me" test
arrives with POST /assistant/ask (task 5.3); these pin the rule and the text.
"""

import pytest

from app.ai.llm import FakeLLM, PermanentLLMError
from app.ai.prompts import (
    DISCLAIMER,
    PROMPT_SAFETY_V1,
    REFUSAL_NO_SERVICE_V1,
    REFUSAL_V1,
)
from app.ai.retrieval import RetrievedService
from app.core.config import settings
from app.models.enums import AssistantIntent
from app.services.assistant import check_safety, refusal, with_disclaimer

ORTHO_CONSULT = RetrievedService(
    service_id=3,
    name="Orthopaedic Consultation",
    department="Orthopaedics",
    specialties=["Orthopaedics"],
    similarity=0.52,
)


async def test_a_diagnose_me_question_is_refused() -> None:
    check = await check_safety(
        "Can you diagnose me? I've had a headache for three days.",
        FakeLLM("medical_advice"),
    )

    assert check.intent is AssistantIntent.MEDICAL_ADVICE
    assert check.refused


@pytest.mark.parametrize(
    "reply",
    ["", "I think this is navigation.", "staff_generation", "diagnosis"],
    ids=["empty", "a-sentence", "a-label-not-offered", "an-unknown-word"],
)
async def test_a_reply_that_names_no_label_is_refused(reply: str) -> None:
    check = await check_safety("Do you offer knee scans?", FakeLLM(reply))

    assert check.intent is None
    assert check.refused


@pytest.mark.parametrize(
    "label", ["navigation", "preparation", "availability", "other"]
)
async def test_every_other_label_is_answered(label: str) -> None:
    check = await check_safety("Do you offer knee scans?", FakeLLM(label))

    assert check.intent is AssistantIntent(label.upper())
    assert not check.refused


async def test_the_safety_check_sends_the_safety_prompt_with_the_question_tagged() -> (
    None
):
    llm = FakeLLM("navigation")

    check = await check_safety("Do you offer knee scans?", llm)

    [(system, user)] = llm.calls
    assert system.content == PROMPT_SAFETY_V1.text.format(
        clinic=settings.assistant_clinic_name
    )
    assert user.content == "<question>\nDo you offer knee scans?\n</question>"
    assert check.prompt_version == PROMPT_SAFETY_V1.version
    assert check.usage.output_tokens == 1


async def test_a_failed_safety_check_raises_instead_of_letting_the_question_through() -> (
    None
):
    llm = FakeLLM(PermanentLLMError("LLM provider returned HTTP 401"))

    with pytest.raises(PermanentLLMError):
        await check_safety("Do you offer knee scans?", llm)


def test_a_refusal_routes_to_the_services_found_and_ends_with_the_disclaimer() -> None:
    reply = refusal([ORTHO_CONSULT])

    assert reply.version == REFUSAL_V1.version
    assert "Orthopaedic Consultation (Orthopaedics)" in reply.text
    assert "urgent care" in reply.text
    assert reply.text.endswith(DISCLAIMER)


def test_a_refusal_with_no_services_suggests_a_consultation_and_the_disclaimer() -> (
    None
):
    reply = refusal([])

    assert reply.version == REFUSAL_NO_SERVICE_V1.version
    assert reply.text.startswith(REFUSAL_NO_SERVICE_V1.text)
    assert reply.text.endswith(DISCLAIMER)


def test_the_disclaimer_goes_after_the_text_on_its_own_line() -> None:
    assert with_disclaimer("Bring prior imaging.") == (
        f"Bring prior imaging.\n\n{DISCLAIMER}"
    )

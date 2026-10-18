"""The patient assistant's safety check and refusal (task 5.4).

POST /assistant/ask (task 5.3) runs check_safety() on every question before
anything else. A refused question gets refusal(): fixed text from
app/ai/prompts.py, never the model's words, so what a patient asking for
medical advice is told does not depend on what a model chooses to write.

Every reply the assistant gives ends with the brief's disclaimer, added by
with_disclaimer() here rather than written by the model.
"""

from dataclasses import dataclass

from app.ai.llm import LLMProvider, LLMUsage
from app.ai.prompts import (
    DISCLAIMER,
    FixedReply,
    build_safety_prompt,
    parse_intent,
    refusal_reply,
)
from app.ai.retrieval import RetrievedService
from app.core.config import settings
from app.models.enums import AssistantIntent

# The longest reply the safety check may produce. The answer is one label,
# a few tokens, so the cap only limits what a rambling model can cost. A
# reply cut short names no label and is refused: the safe side.
SAFETY_MAX_TOKENS = 16


@dataclass(frozen=True)
class SafetyCheck:
    """What the safety check made of one question, and what it cost."""

    intent: AssistantIntent | None
    prompt_version: str
    usage: LLMUsage

    @property
    def refused(self) -> bool:
        """Whether the question gets the refusal instead of an answer.

        MEDICAL_ADVICE is refused, and so is no label (None). A reply that
        is not a label means the model could not sort the question, so
        nothing shows the question is safe to answer.
        """
        return self.intent is None or self.intent is AssistantIntent.MEDICAL_ADVICE


async def check_safety(question: str, llm: LLMProvider) -> SafetyCheck:
    """Ask the model which AssistantIntent `question` is.

    A failed call raises LLMError rather than returning a label: with no
    safety check there is no answer, and the caller tells the patient the
    assistant is unavailable.
    """
    prompt = build_safety_prompt(question, clinic=settings.assistant_clinic_name)
    result = await llm.complete(prompt.messages, max_tokens=SAFETY_MAX_TOKENS)
    return SafetyCheck(
        intent=parse_intent(result.text),
        prompt_version=prompt.version,
        usage=result.usage,
    )


def refusal(services: list[RetrievedService]) -> FixedReply:
    """The reply to a refused question, ending with the disclaimer.

    `services` are the offered services retrieval found for the question.
    The refusal names them, so the patient knows where a clinician can
    help; with none, it suggests a consultation.
    """
    reply = refusal_reply(services)
    return FixedReply(reply.version, with_disclaimer(reply.text))


def with_disclaimer(text: str) -> str:
    """`text`, then the brief's disclaimer on its own line."""
    return f"{text}\n\n{DISCLAIMER}"

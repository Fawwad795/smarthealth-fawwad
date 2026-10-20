"""POST /assistant/ask: the patient assistant's request and reply (tasks
5.3 and 5.6).

The request's checks run here, in the schema, before any AI work, so a
question that can never get a useful answer is a 422 that costs no
embedding and no model call.
"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from app.models.enums import AssistantIntent, InteractionOutcome
from app.schemas.search import SearchQuery


def _has_a_letter(question: str) -> str:
    """Reject a question with no letter in it, such as "!!!!" or "12345".

    Text without a letter cannot name a service or ask about one.
    isalpha() counts letters in any alphabet, not only a to z.

    Gibberish shaped like words ("asdkjh qwe zzz") passes on purpose: no
    simple rule tells it from a real word the rule has never seen, such as
    a service's name. Retrieval catches it instead: nothing scores above
    the threshold, and the patient is told the clinic does not offer it.
    """
    if not any(char.isalpha() for char in question):
        raise ValueError("the question must contain at least one letter")
    return question


# Week 4's search query rules (spaces stripped, then 1 to
# AI_MAX_QUESTION_CHARS long), then the letter check on the stripped text.
AssistantQuestion = Annotated[SearchQuery, AfterValidator(_has_a_letter)]


class AskRequest(BaseModel):
    """A patient's question for the assistant."""

    question: AssistantQuestion = Field(
        description="The patient's question, in their own words.",
        examples=["Do you offer knee scans?"],
    )


class Citation(BaseModel):
    """One offered service the reply was built from. Service fields only."""

    service_id: int
    name: str
    department: str
    similarity: float = Field(
        description="Cosine similarity to the question, from 0 to 1, rounded to 3 places."
    )


class AskResponse(BaseModel):
    """The assistant's reply, how the question ended, and what it drew on.

    No field repeats the question: the patient sent it, and echoing it back
    only adds another place their words appear.
    """

    answer: str = Field(description="The reply. It always ends with the disclaimer.")
    intent: AssistantIntent | None = Field(
        description="The safety check's label. Null when it gave none, which is refused."
    )
    outcome: InteractionOutcome = Field(
        description="ANSWERED, REFUSED (medical advice) or NOT_OFFERED (nothing matched)."
    )
    citations: list[Citation] = Field(
        description="The offered services the reply was built from, most similar first."
    )
    interaction_id: int = Field(
        description="The ai_interactions row recording this question, for tracing an answer."
    )

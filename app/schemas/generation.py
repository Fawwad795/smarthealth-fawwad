"""Staff generation: the utilisation report's shape and the events of a
generation stream (task 5.9).

The report is two halves with different sources. UtilisationFigures holds
every number, read from the database by the code. ReportCommentary is the
only part the model writes, and it is validated before it is used: JSON
with exactly its two fields, and no digits anywhere, so the model cannot
put a number of its own into the report.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import GenerationKind, InteractionOutcome
from app.schemas.analytics import AnalyticsSummaryResponse

SUMMARY_MAX_CHARS = 600
OBSERVATION_MAX_CHARS = 300
MAX_OBSERVATIONS = 4


class DepartmentUtilisation(BaseModel):
    """How much of one department's bookable time was booked.

    Counted from the slots table: analytics_daily is one row per day for
    the whole clinic, with no department.
    """

    department_id: int
    department: str
    slots: int = Field(
        description="Slots of the department's providers starting in the range; BLOCKED ones left out."
    )
    booked_slots: int = Field(description="Of those, the slots that are BOOKED.")
    utilisation: float | None = Field(
        description="booked_slots / slots, rounded to 3 places. Null with no slots."
    )


class UtilisationFigures(BaseModel):
    """Every number in a utilisation report, as the code read it.

    `analytics` is exactly what GET /analytics/summary returns for the same
    range, built by the same function, so the report's figures match the
    analytics tables by construction.
    """

    analytics: AnalyticsSummaryResponse
    departments: list[DepartmentUtilisation]


class ReportCommentary(BaseModel):
    """What the model writes for a utilisation report: words, no numbers.

    extra="forbid" rejects any other key, so the model cannot slip a
    figures object of its own into the reply.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=SUMMARY_MAX_CHARS)
    observations: list[str] = Field(min_length=1, max_length=MAX_OBSERVATIONS)

    @field_validator("observations")
    @classmethod
    def _observations_are_short_sentences(cls, observations: list[str]) -> list[str]:
        """Each observation is a sentence: not empty, not an essay."""
        for observation in observations:
            if not observation.strip():
                raise ValueError("an observation is empty")
            if len(observation) > OBSERVATION_MAX_CHARS:
                raise ValueError(
                    f"an observation is longer than {OBSERVATION_MAX_CHARS} characters"
                )
        return observations

    @field_validator("summary", "observations")
    @classmethod
    def _no_digits(cls, value: str | list[str]) -> str | list[str]:
        """Reject any digit. Every number in the report comes from the
        figures, which the code prints beside this text; a digit here
        would be one the model wrote itself, and could be wrong."""
        texts = [value] if isinstance(value, str) else value
        if any(char.isdigit() for text in texts for char in text):
            raise ValueError("contains a digit; refer to figures in words")
        return value


class UtilisationReport(BaseModel):
    """A validated utilisation report: the code's figures and the model's
    commentary. Saved in generated_content as it is."""

    figures: UtilisationFigures
    summary: str
    observations: list[str]


# POST /reports/generate/utilisation's body is DateRange from
# app/schemas/analytics.py: the same defaults and limits as
# GET /analytics/summary, for the same figures.


# --- Stream events -----------------------------------------------------------
#
# A summary or follow-up stream is `token` events, then `citations`, then
# `done`. A report stream is `token` events (the model's JSON as it is
# written), a `retry` event and more tokens if the first reply could not be
# used, then `report`, then `done`. If generation fails, `error` replaces
# `citations` or `report`.


class GenerationRetry(BaseModel):
    """The `retry` event: the report reply could not be used, so the model
    is asked once more. The tokens after it are the new reply."""

    reason: str


class GenerationCitations(BaseModel):
    """The `citations` event of a summary or follow-up: what it was built
    from."""

    appointment_id: int
    service_id: int


class GenerationDone(BaseModel):
    """The `done` event, always the last."""

    kind: GenerationKind
    outcome: InteractionOutcome = Field(
        description="ANSWERED, or FAILED after an error event."
    )
    generated_content_id: int
    interaction_id: int

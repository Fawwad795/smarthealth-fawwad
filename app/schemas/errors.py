"""The error envelope, as a schema, so Swagger documents failures too.

app/core/error_handlers.py builds this shape at runtime for every failure.
These models exist purely so /docs shows it as well: without them a reader
sees only the success response and has to guess what a 409 looks like.
"""

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """The inner object every failure carries."""

    code: str = Field(
        description="Stable machine-readable code a client can branch on.",
        examples=["SLOT_UNAVAILABLE"],
    )
    message: str = Field(
        description=(
            "Human-readable text, always safe to show. Never carries PHI, "
            "a stack trace or internal detail."
        ),
        examples=["The new slot is no longer available."],
    )


class ErrorResponse(BaseModel):
    """Every non-2xx response in this API has exactly this shape."""

    error: ErrorDetail


def error_responses(mapping: dict[int, str]) -> dict[int | str, dict]:
    """Build FastAPI's `responses=` map from {status_code: description}.

    Saves repeating the model reference on every route, and keeps each
    documented failure next to the route that actually produces it.
    """
    return {
        code: {"model": ErrorResponse, "description": description}
        for code, description in mapping.items()
    }

"""POST /search: semantic search over the published services (task 4.8).

Separate from service_search.py, Week 1's keyword catalogue: that one
matches words in a service's name, this one compares meanings, and returns
a similarity score with each result.
"""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.core.config import settings

# Whitespace is stripped before the length check, so a query of only spaces
# is as empty as "" and is a 422, not a search for nothing.
SearchQuery = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=settings.ai_max_question_chars
    ),
]


class SearchRequest(BaseModel):
    """What to search for, and at most how many services to return.

    No threshold field: the minimum similarity is server configuration
    (RETRIEVAL_MIN_SIMILARITY), so no client can ask for weak matches.
    """

    query: SearchQuery = Field(
        description="What the patient is looking for, in their own words.",
        examples=["knee pain"],
    )
    top_k: int = Field(
        default=settings.retrieval_top_k,
        ge=1,
        le=20,
        description="The most services to return. Fewer come back when fewer are similar enough.",
    )


class SearchResult(BaseModel):
    """One matching service. Service fields only: nothing a search returns
    is about a patient."""

    service_id: int
    name: str
    department: str
    specialties: list[str]
    similarity: float = Field(
        description="Cosine similarity to the query, from 0 (unrelated) to 1, rounded to 3 places."
    )


class SearchResponse(BaseModel):
    """The matches, most similar first, and the threshold they all cleared.

    An empty `results` is a real answer, not an error: nothing the clinic
    offers is close enough, and `message` says so.
    """

    results: list[SearchResult]
    min_similarity: float = Field(
        description="The threshold applied: every result scored at least this."
    )
    message: str | None = Field(
        default=None,
        description="Set when nothing matched: the clinic does not offer it.",
    )

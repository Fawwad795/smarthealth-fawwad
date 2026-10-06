"""The one test that calls Hugging Face for real.

Skipped by default: the suite must pass with no network and no key. Run it
on purpose with

    docker compose run --rm -e RUN_LIVE_TESTS=1 test pytest -m live

It proves what MockTransport cannot: that the real endpoint, model and key
still agree with the shape HuggingFaceEmbeddings expects.
"""

import math
import os

import pytest

from app.ai.embeddings import HuggingFaceEmbeddings, get_embedding_provider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="calls Hugging Face; set RUN_LIVE_TESTS=1 to run",
    ),
]


def test_hugging_face_returns_unit_vectors_that_rank_by_meaning() -> None:
    provider = get_embedding_provider()
    assert isinstance(provider, HuggingFaceEmbeddings)

    query, knee, skin = provider.embed(
        [
            "I need a scan of my knee",
            "Orthopaedics · Orthopedics: Knee X-Ray. Standard imaging of the knee joint.",
            "Dermatology: Full Skin Check. A full-body skin examination.",
        ]
    )

    for vector in (query, knee, skin):
        assert len(vector) == provider.dimensions
        assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, abs_tol=1e-3)
    # All three are unit length, so the dot product is the cosine similarity.
    assert _dot(query, knee) > _dot(query, skin)


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

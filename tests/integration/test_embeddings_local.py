"""LocalEmbeddings against the real model the image bakes in.

No network: the Dockerfile downloads all-MiniLM-L6-v2 at build time and sets
HF_HUB_OFFLINE, so these tests read it from disk. Skipped outside the image,
where loading the model would mean a download.
"""

import math
import os

import pytest

from app.ai.embeddings import LocalEmbeddings
from app.core.config import Settings

pytestmark = pytest.mark.skipif(
    not os.getenv("EMBEDDING_MODEL_DIR"),
    reason="the model is baked into the Docker image",
)

KNEE_CHUNK = (
    "Orthopaedics · Orthopedics: Knee X-Ray. Standard imaging of the knee joint. "
    "Preparation: Wear loose clothing; remove any metal jewellery near the knee."
)
ECHO_CHUNK = (
    "Cardiology: Echocardiogram. Ultrasound imaging of the heart. "
    "Preparation: No special preparation required. Arrive 10 minutes early."
)


@pytest.fixture(scope="module")
def provider() -> LocalEmbeddings:
    return LocalEmbeddings(
        model=Settings.model_fields["embedding_model"].default,
        dimensions=384,
        batch_size=64,
    )


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_local_model_returns_384_numbers_of_length_one(
    provider: LocalEmbeddings,
) -> None:
    [vector] = provider.embed([KNEE_CHUNK])

    assert len(vector) == 384
    assert math.isclose(math.sqrt(_dot(vector, vector)), 1.0, abs_tol=1e-3)


def test_local_model_gives_the_same_scores_as_the_hosted_one(
    provider: LocalEmbeddings,
) -> None:
    """Week 4 Day 1 measured 0.614 and 0.221 for this query through Hugging
    Face's hosted API. The same scores here mean the same model, so
    switching providers changed where it runs, not what search returns.
    Vectors are length 1, so the dot product is the cosine similarity.
    """
    query, knee, echo = provider.embed(
        ["I need a scan of my knee", KNEE_CHUNK, ECHO_CHUNK]
    )

    assert _dot(query, knee) == pytest.approx(0.614, abs=0.001)
    assert _dot(query, echo) == pytest.approx(0.221, abs=0.001)

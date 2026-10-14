"""POST /search through the real FastAPI routing layer (task 4.8).

The embedding provider is FakeEmbeddings, swapped in through
app.dependency_overrides, so nothing reaches the network or loads a model.
The fake ranks by shared words, so a query that shares words with a
service's text scores high and one that shares none scores 0. Services are
indexed by hand, the way a finished publish leaves them; the publish
workflow itself is covered by test_service_reindex.py.

The threshold is fixed at 0.3 here, so the tests do not move when task
4.10 chooses the real value.
"""

import logging
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.ai import embeddings as embeddings_module
from app.ai.embeddings import (
    EmbeddingProvider,
    FakeEmbeddings,
    TransientEmbeddingError,
)
from app.ai.retrieval import get_query_embeddings
from app.ai.vector_store import PgVectorStore, VectorRecord
from app.core.config import settings
from app.main import app
from app.models import ContentChunk, Department, Patient, Service
from app.models.enums import ContentSourceType, ServiceStatus

THRESHOLD = 0.3

KNEE_TEXT = "Orthopaedics: Knee X-Ray. Imaging of the knee joint."
SKIN_TEXT = "Dermatology: Skin Check. A look at moles and rashes."


@pytest.fixture(autouse=True)
def _fixed_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "retrieval_min_similarity", THRESHOLD)


@pytest.fixture()
def use_provider(client: TestClient) -> Callable[[EmbeddingProvider], None]:
    """Make POST /search embed with the given provider. The client
    fixture clears every override when the test ends."""

    def _use(provider: EmbeddingProvider) -> None:
        app.dependency_overrides[get_query_embeddings] = lambda: provider

    return _use


@pytest.fixture()
def fake_search(use_provider: Callable[[EmbeddingProvider], None]) -> None:
    use_provider(FakeEmbeddings())


def _indexed(
    db: Session,
    department: Department,
    name: str,
    text: str,
    *,
    status: ServiceStatus = ServiceStatus.PUBLISHED,
) -> Service:
    """A service with one chunk of `text` and its fake vector. The vector is
    published only if the service is, as the publish workflow leaves it."""
    service = Service(department_id=department.id, name=name, status=status)
    db.add(service)
    db.flush()
    chunk = ContentChunk(
        source_type=ContentSourceType.SERVICE,
        source_id=service.id,
        chunk_index=0,
        text=text,
        token_count=len(text) // 4,
        text_hash="0" * 64,
    )
    db.add(chunk)
    db.flush()
    fake = FakeEmbeddings()
    store = PgVectorStore(db)
    store.store_service_vectors(
        service.id,
        [
            VectorRecord(
                chunk_id=chunk.id,
                embedding=fake.embed([text])[0],
                model=fake.model,
                department=department.name,
                specialties=["Orthopedics"],
            )
        ],
        [chunk.id],
    )
    store.set_published(service.id, status == ServiceStatus.PUBLISHED)
    return service


def _search(client: TestClient, headers: dict, **body: object):
    return client.post("/api/v1/search", headers=headers, json=body)


def test_search_returns_matching_services_most_similar_first(
    client: TestClient,
    db_session: Session,
    department: Department,
    patient_auth_headers: dict,
    fake_search: None,
) -> None:
    knee = _indexed(db_session, department, "Knee X-Ray", KNEE_TEXT)
    _indexed(db_session, department, "Skin Check", SKIN_TEXT)
    ankle = _indexed(
        db_session, department, "Ankle X-Ray", "Orthopaedics: Ankle X-Ray."
    )

    response = _search(client, patient_auth_headers, query="knee x-ray")

    assert response.status_code == 200
    body = response.json()
    assert [r["service_id"] for r in body["results"]] == [knee.id, ankle.id]
    first = body["results"][0]
    assert set(first) == {
        "service_id",
        "name",
        "department",
        "specialties",
        "similarity",
    }
    assert first["name"] == "Knee X-Ray"
    assert first["department"] == department.name
    assert first["specialties"] == ["Orthopedics"]
    assert first["similarity"] == 0.696
    assert body["min_similarity"] == THRESHOLD
    assert body["message"] is None


@pytest.mark.parametrize(
    "status",
    [
        ServiceStatus.DRAFT,
        ServiceStatus.PUBLISHING,
        ServiceStatus.PUBLISH_FAILED,
        ServiceStatus.INACTIVE,
    ],
)
def test_search_never_returns_a_service_that_is_not_published(
    client: TestClient,
    db_session: Session,
    department: Department,
    patient_auth_headers: dict,
    fake_search: None,
    status: ServiceStatus,
) -> None:
    """Same text as a published service, so it would score the same. Only
    the published one comes back: a draft or withdrawn service is never
    recommended."""
    published = _indexed(db_session, department, "Knee X-Ray", KNEE_TEXT)
    _indexed(db_session, department, "Knee X-Ray (old)", KNEE_TEXT, status=status)

    response = _search(client, patient_auth_headers, query="knee x-ray")

    assert [r["service_id"] for r in response.json()["results"]] == [published.id]


def test_search_with_nothing_above_the_threshold_says_we_dont_offer_it(
    client: TestClient,
    db_session: Session,
    department: Department,
    patient_auth_headers: dict,
    fake_search: None,
) -> None:
    """Weak matches are not shown: an empty list and a message, not noise."""
    _indexed(db_session, department, "Knee X-Ray", KNEE_TEXT)

    response = _search(client, patient_auth_headers, query="hair transplant")

    assert response.status_code == 200
    assert response.json()["results"] == []
    assert response.json()["message"].startswith("We don't offer a service")


def test_search_returns_at_most_top_k(
    client: TestClient,
    db_session: Session,
    department: Department,
    patient_auth_headers: dict,
    fake_search: None,
) -> None:
    for part in ("Knee", "Ankle", "Wrist"):
        _indexed(db_session, department, f"{part} X-Ray", f"{part} X-Ray.")

    response = _search(client, patient_auth_headers, query="x-ray", top_k=2)

    assert len(response.json()["results"]) == 2


def test_search_returns_five_by_default(
    client: TestClient,
    db_session: Session,
    department: Department,
    patient_auth_headers: dict,
    fake_search: None,
) -> None:
    for part in ("Knee", "Ankle", "Wrist", "Hip", "Elbow", "Spine"):
        _indexed(db_session, department, f"{part} X-Ray", f"{part} X-Ray.")

    response = _search(client, patient_auth_headers, query="x-ray")

    assert len(response.json()["results"]) == 5


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 1001},
        {"query": "knee", "top_k": 0},
        {"query": "knee", "top_k": 21},
    ],
    ids=["missing", "empty", "spaces", "too-long", "top_k-0", "top_k-21"],
)
def test_search_rejects_a_bad_request_with_422(
    client: TestClient, patient_auth_headers: dict, fake_search: None, body: dict
) -> None:
    response = client.post("/api/v1/search", headers=patient_auth_headers, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_search_requires_sign_in(client: TestClient, fake_search: None) -> None:
    response = client.post("/api/v1/search", json={"query": "knee"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        "patient_auth_headers",
        "provider_auth_headers",
        "front_desk_auth_headers",
        "admin_auth_headers",
    ],
)
def test_search_is_open_to_every_signed_in_role(
    client: TestClient,
    fake_search: None,
    request: pytest.FixtureRequest,
    headers: str,
) -> None:
    response = _search(client, request.getfixturevalue(headers), query="knee")

    assert response.status_code == 200


class _UnreachableEmbeddings(EmbeddingProvider):
    """A provider whose every call fails as a timeout would."""

    def __init__(self) -> None:
        super().__init__(model="unreachable", dimensions=384)

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise TransientEmbeddingError("Hugging Face unreachable (ConnectTimeout)")


@pytest.mark.parametrize(
    "provider",
    [_UnreachableEmbeddings(), FakeEmbeddings(dimensions=16)],
    ids=["provider-down", "wrong-vector-size"],
)
def test_search_is_503_when_the_query_cannot_be_embedded(
    client: TestClient,
    patient_auth_headers: dict,
    use_provider: Callable[[EmbeddingProvider], None],
    provider: EmbeddingProvider,
) -> None:
    """Search is unavailable, nothing is wrong with the request; the
    provider's own message stays in the log."""
    use_provider(provider)

    response = _search(client, patient_auth_headers, query="knee")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SEARCH_UNAVAILABLE"
    assert "Hugging Face" not in response.text


def test_search_is_503_when_the_provider_cannot_be_built(
    client: TestClient,
    patient_auth_headers: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real dependency, with the hosted provider and no key."""
    monkeypatch.setattr(embeddings_module.settings, "embedding_provider", "huggingface")
    monkeypatch.setattr(embeddings_module.settings, "embedding_api_key", SecretStr(""))

    response = _search(client, patient_auth_headers, query="knee")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SEARCH_UNAVAILABLE"


# --- PHI ---------------------------------------------------------------------
#
# Service search has no patient data to scope: the index is built from
# service, department and specialty fields only (app/ai/chunking.py). These
# tests pin that down from the outside. Scoping a patient's own appointments
# to the caller belongs to the Week 5 assistant, the first retrieval that
# touches them.


def test_a_patients_details_cannot_be_found_through_search(
    client: TestClient,
    db_session: Session,
    department: Department,
    patient: Patient,
    patient_auth_headers: dict,
    fake_search: None,
) -> None:
    """A synthetic patient with a distinctive name and phone number exists
    alongside a published service. Searching for them finds nothing, and
    nothing of theirs is in the response."""
    patient.contact = {"name": "Zebulon Quixote", "phone": "+1-555-0100"}
    db_session.flush()
    _indexed(db_session, department, "Knee X-Ray", KNEE_TEXT)

    response = _search(client, patient_auth_headers, query="Zebulon Quixote 555 0100")

    assert response.json()["results"] == []
    assert "Zebulon" not in response.text
    assert "0100" not in response.text


def test_search_logs_result_ids_but_never_the_query(
    client: TestClient,
    db_session: Session,
    department: Department,
    patient_auth_headers: dict,
    fake_search: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A query is the patient's own words and can name a condition or a
    person. The log line carries only the ids of what came back."""
    knee = _indexed(db_session, department, "Knee X-Ray", KNEE_TEXT)
    caplog.set_level(logging.INFO, logger="app.ai.retrieval")

    _search(client, patient_auth_headers, query="knee x-ray for Zebulon")

    assert f"search returned 1 services: [{knee.id}]" in caplog.text
    assert "Zebulon" not in caplog.text

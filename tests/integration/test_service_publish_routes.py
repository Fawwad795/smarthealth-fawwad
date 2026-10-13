"""POST /services/{id}/publish and GET /services/{id}/publish-status
through the real FastAPI routing layer.

publish_service calls the real Temporal client -- these tests replace it
with a fake (monkeypatched where service_publish.py uses it, not where
get_temporal_client is defined) so they prove the endpoint's own logic
(role, guard, status transition, response shape), not Temporal
connectivity. The real workflow is already proven by
test_publish_workflow.py and the live worker check.

publish-status asks Temporal for a running publish's progress through
describe(); the fake answers with whatever pending Activities a test sets,
shaped like the protobuf fields the real description carries.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.vector_store import PgVectorStore, VectorRecord
from app.models import ChunkEmbedding, ContentChunk, Department, Service
from app.models.chunk_embedding import EMBEDDING_DIMENSIONS
from app.models.enums import ContentSourceType, ServiceStatus
from app.services import service_publish


def _make_service(
    db_session: Session,
    department: Department,
    name: str,
    status: ServiceStatus = ServiceStatus.DRAFT,
) -> Service:
    s = Service(department_id=department.id, name=name, status=status)
    db_session.add(s)
    db_session.flush()
    return s


def _give_published_vector(db_session: Session, service: Service) -> None:
    """One chunk with a searchable vector, as a finished publish leaves it."""
    chunk = ContentChunk(
        source_type=ContentSourceType.SERVICE,
        source_id=service.id,
        chunk_index=0,
        text="some text",
        token_count=2,
        text_hash="0" * 64,
    )
    db_session.add(chunk)
    db_session.flush()
    store = PgVectorStore(db_session)
    store.store_service_vectors(
        service.id,
        [
            VectorRecord(
                chunk_id=chunk.id,
                embedding=[1.0] * EMBEDDING_DIMENSIONS,
                model="test-model",
                department="Orthopaedics",
                specialties=[],
            )
        ],
        [chunk.id],
    )
    store.set_published(service.id, True)


def _vector_flags(db_session: Session, service: Service) -> list[bool]:
    """The published flag of each of the service's vectors, from Postgres."""
    db_session.expire_all()
    return list(
        db_session.scalars(
            select(ChunkEmbedding.published).where(
                ChunkEmbedding.service_id == service.id
            )
        )
    )


def _pending(step: str, attempt: int, last_error: str = "") -> SimpleNamespace:
    """One pending Activity as describe() reports it. An unset last_failure
    reads as an empty message, as an unset protobuf field does."""
    return SimpleNamespace(
        activity_type=SimpleNamespace(name=step),
        attempt=attempt,
        last_failure=SimpleNamespace(message=last_error),
    )


class _FakeWorkflowHandle:
    """Answers describe() with the client's pending Activities."""

    def __init__(self, pending_activities: list[SimpleNamespace]) -> None:
        self._pending_activities = pending_activities

    async def describe(self) -> SimpleNamespace:
        return SimpleNamespace(
            raw_description=SimpleNamespace(pending_activities=self._pending_activities)
        )


class _FakeTemporalClient:
    """Records what it was asked to start or describe, and answers describe()
    with `pending_activities`."""

    def __init__(self) -> None:
        self.started_with: list[tuple[Any, ...]] = []
        self.described: list[str] = []
        self.pending_activities: list[SimpleNamespace] = []

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        self.started_with.append((args, kwargs))

    def get_workflow_handle(self, workflow_id: str) -> _FakeWorkflowHandle:
        self.described.append(workflow_id)
        return _FakeWorkflowHandle(self.pending_activities)


@pytest.fixture()
def fake_temporal_client(monkeypatch: pytest.MonkeyPatch) -> _FakeTemporalClient:
    fake_client = _FakeTemporalClient()

    async def _fake_get_temporal_client() -> _FakeTemporalClient:
        return fake_client

    monkeypatch.setattr(
        "app.services.service_publish.get_temporal_client", _fake_get_temporal_client
    )
    return fake_client


def test_publish_service_returns_202_and_transitions_to_publishing(
    client: TestClient,
    db_session: Session,
    department: Department,
    admin_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    service = _make_service(db_session, department, "Knee X-Ray")

    response = client.post(
        f"/api/v1/services/{service.id}/publish", headers=admin_auth_headers
    )

    assert response.status_code == 202
    body = response.json()
    assert body["service_id"] == service.id
    assert body["status"] == "PUBLISHING"
    assert body["workflow_id"] == f"publish-service-{service.id}"
    assert len(fake_temporal_client.started_with) == 1


def test_publish_service_forbidden_for_non_admin_staff(
    client: TestClient,
    db_session: Session,
    department: Department,
    provider_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    service = _make_service(db_session, department, "Knee X-Ray")

    response = client.post(
        f"/api/v1/services/{service.id}/publish", headers=provider_auth_headers
    )

    assert response.status_code == 403
    assert fake_temporal_client.started_with == []


def test_publish_service_not_found_returns_404(
    client: TestClient,
    admin_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    response = client.post(
        "/api/v1/services/999999/publish", headers=admin_auth_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SERVICE_NOT_FOUND"


def test_publish_service_already_publishing_returns_409(
    client: TestClient,
    db_session: Session,
    department: Department,
    admin_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    """PUBLISHING is the one status publish refuses: a workflow is already
    running against the row."""
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHING
    )

    response = client.post(
        f"/api/v1/services/{service.id}/publish", headers=admin_auth_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SERVICE_NOT_PUBLISHABLE"
    assert fake_temporal_client.started_with == []


def test_get_publish_status_returns_current_state(
    client: TestClient,
    db_session: Session,
    department: Department,
    provider_auth_headers: dict,
) -> None:
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED
    )

    response = client.get(
        f"/api/v1/services/{service.id}/publish-status",
        headers=provider_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["service_id"] == service.id
    assert body["status"] == "PUBLISHED"
    assert body["workflow_id"] == f"publish-service-{service.id}"


def test_get_publish_status_not_found_returns_404(
    client: TestClient, provider_auth_headers: dict
) -> None:
    response = client.get(
        "/api/v1/services/999999/publish-status", headers=provider_auth_headers
    )

    assert response.status_code == 404


def test_get_publish_status_forbidden_for_patient(
    client: TestClient,
    db_session: Session,
    department: Department,
    patient_auth_headers: dict,
) -> None:
    service = _make_service(db_session, department, "Knee X-Ray")

    response = client.get(
        f"/api/v1/services/{service.id}/publish-status",
        headers=patient_auth_headers,
    )

    assert response.status_code == 403


def test_get_publish_status_shows_a_retrying_step_while_publishing(
    client: TestClient,
    db_session: Session,
    department: Department,
    provider_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    """The embedding provider is failing and Temporal is retrying: the
    status shows which step, which attempt, and the last error."""
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHING
    )
    fake_temporal_client.pending_activities = [
        _pending("embed_chunks", 3, "Hugging Face returned HTTP 503")
    ]

    response = client.get(
        f"/api/v1/services/{service.id}/publish-status",
        headers=provider_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["progress"] == {
        "step": "embed_chunks",
        "attempt": 3,
        "last_error": "Hugging Face returned HTTP 503",
    }
    assert fake_temporal_client.described == [f"publish-service-{service.id}"]


def test_get_publish_status_first_attempt_has_no_last_error(
    client: TestClient,
    db_session: Session,
    department: Department,
    provider_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHING
    )
    fake_temporal_client.pending_activities = [_pending("chunk_content", 1)]

    response = client.get(
        f"/api/v1/services/{service.id}/publish-status",
        headers=provider_auth_headers,
    )

    assert response.json()["progress"] == {
        "step": "chunk_content",
        "attempt": 1,
        "last_error": None,
    }


def test_get_publish_status_between_steps_has_no_progress(
    client: TestClient,
    db_session: Session,
    department: Department,
    provider_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    """Between two Activities nothing is pending; that is not an error."""
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHING
    )

    response = client.get(
        f"/api/v1/services/{service.id}/publish-status",
        headers=provider_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["progress"] is None


def test_get_publish_status_says_why_a_publish_failed(
    client: TestClient,
    db_session: Session,
    department: Department,
    provider_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    """The reason comes off the row, so Temporal is not asked."""
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISH_FAILED
    )
    service.publish_error = "Hugging Face returned HTTP 401"
    db_session.flush()

    response = client.get(
        f"/api/v1/services/{service.id}/publish-status",
        headers=provider_auth_headers,
    )

    body = response.json()
    assert body["status"] == "PUBLISH_FAILED"
    assert body["publish_error"] == "Hugging Face returned HTTP 401"
    assert body["progress"] is None
    assert fake_temporal_client.described == []


def test_get_publish_status_still_answers_when_temporal_is_down(
    client: TestClient,
    db_session: Session,
    department: Department,
    provider_auth_headers: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client.connect raises RuntimeError when the server is unreachable.
    The row's own status is still worth returning."""

    async def _unreachable() -> None:
        raise RuntimeError("Failed client connect")

    monkeypatch.setattr(
        "app.services.service_publish.get_temporal_client", _unreachable
    )
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHING
    )

    response = client.get(
        f"/api/v1/services/{service.id}/publish-status",
        headers=provider_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "PUBLISHING"
    assert response.json()["progress"] is None


def test_publish_service_clears_the_last_failure_reason(
    client: TestClient,
    db_session: Session,
    department: Department,
    admin_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISH_FAILED
    )
    service.publish_error = "Hugging Face returned HTTP 401"
    db_session.flush()

    response = client.post(
        f"/api/v1/services/{service.id}/publish", headers=admin_auth_headers
    )

    assert response.status_code == 202
    db_session.refresh(service)
    assert service.publish_error is None


async def test_start_publish_restores_status_and_reason_if_temporal_refuses(
    db_session: Session,
    department: Department,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the workflow never starts, the row must not claim it is
    PUBLISHING, and the last failure's reason must still be there."""

    class _RefusingClient:
        async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Temporal is down")

    async def _refusing() -> _RefusingClient:
        return _RefusingClient()

    monkeypatch.setattr("app.services.service_publish.get_temporal_client", _refusing)
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISH_FAILED
    )
    service.publish_error = "Hugging Face returned HTTP 401"
    db_session.flush()

    with pytest.raises(RuntimeError, match="Temporal is down"):
        await service_publish.start_publish(db_session, service.id)

    db_session.refresh(service)
    assert service.status == ServiceStatus.PUBLISH_FAILED
    assert service.publish_error == "Hugging Face returned HTTP 401"


@pytest.mark.parametrize("status", [ServiceStatus.PUBLISHED, ServiceStatus.INACTIVE])
def test_publish_a_service_again_returns_202_and_takes_its_vectors_out_of_search(
    client: TestClient,
    db_session: Session,
    department: Department,
    admin_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
    status: ServiceStatus,
) -> None:
    """Task 4.7: publishing again goes straight to PUBLISHING. The old
    vectors leave search at once; mark_published brings the new ones in."""
    service = _make_service(db_session, department, "Knee X-Ray", status)
    _give_published_vector(db_session, service)

    response = client.post(
        f"/api/v1/services/{service.id}/publish", headers=admin_auth_headers
    )

    assert response.status_code == 202
    assert response.json()["status"] == "PUBLISHING"
    assert len(fake_temporal_client.started_with) == 1
    assert _vector_flags(db_session, service) == [False]


async def test_start_publish_restores_the_vectors_if_temporal_refuses_a_republish(
    db_session: Session,
    department: Department,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the workflow never starts, a PUBLISHED service must stay in
    search: its status and its vectors' flag both go back."""

    class _RefusingClient:
        async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Temporal is down")

    async def _refusing() -> _RefusingClient:
        return _RefusingClient()

    monkeypatch.setattr("app.services.service_publish.get_temporal_client", _refusing)
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED
    )
    _give_published_vector(db_session, service)

    with pytest.raises(RuntimeError, match="Temporal is down"):
        await service_publish.start_publish(db_session, service.id)

    db_session.refresh(service)
    assert service.status == ServiceStatus.PUBLISHED
    assert _vector_flags(db_session, service) == [True]


def test_unpublish_service_returns_200_inactive_and_takes_it_out_of_search(
    client: TestClient,
    db_session: Session,
    department: Department,
    admin_auth_headers: dict,
) -> None:
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED
    )
    _give_published_vector(db_session, service)

    response = client.post(
        f"/api/v1/services/{service.id}/unpublish", headers=admin_auth_headers
    )

    assert response.status_code == 200
    assert response.json()["status"] == "INACTIVE"
    assert _vector_flags(db_session, service) == [False]


def test_unpublish_service_keeps_its_vectors_and_published_at(
    client: TestClient,
    db_session: Session,
    department: Department,
    admin_auth_headers: dict,
) -> None:
    """Kept, so publishing it again re-embeds nothing unchanged (task 4.9);
    published_at still says when it was last published."""
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED
    )
    service.published_at = datetime(2026, 10, 1, tzinfo=UTC)
    _give_published_vector(db_session, service)

    response = client.post(
        f"/api/v1/services/{service.id}/unpublish", headers=admin_auth_headers
    )

    assert response.json()["published_at"] == "2026-10-01T00:00:00Z"
    assert len(_vector_flags(db_session, service)) == 1


@pytest.mark.parametrize(
    "status",
    [
        ServiceStatus.DRAFT,
        ServiceStatus.PUBLISHING,
        ServiceStatus.PUBLISH_FAILED,
        ServiceStatus.INACTIVE,
    ],
)
def test_unpublish_service_that_is_not_published_returns_409(
    client: TestClient,
    db_session: Session,
    department: Department,
    admin_auth_headers: dict,
    status: ServiceStatus,
) -> None:
    """INACTIVE included: a second unpublish is refused, not repeated."""
    service = _make_service(db_session, department, "Knee X-Ray", status)

    response = client.post(
        f"/api/v1/services/{service.id}/unpublish", headers=admin_auth_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SERVICE_NOT_UNPUBLISHABLE"
    db_session.refresh(service)
    assert service.status == status


def test_unpublish_service_forbidden_for_non_admin_staff(
    client: TestClient,
    db_session: Session,
    department: Department,
    front_desk_auth_headers: dict,
) -> None:
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED
    )

    response = client.post(
        f"/api/v1/services/{service.id}/unpublish", headers=front_desk_auth_headers
    )

    assert response.status_code == 403
    db_session.refresh(service)
    assert service.status == ServiceStatus.PUBLISHED


def test_unpublish_service_not_found_returns_404(
    client: TestClient, admin_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/services/999999/unpublish", headers=admin_auth_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SERVICE_NOT_FOUND"

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

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Department, Service
from app.models.enums import ServiceStatus
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


def test_publish_service_wrong_status_returns_409(
    client: TestClient,
    db_session: Session,
    department: Department,
    admin_auth_headers: dict,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    service = _make_service(
        db_session, department, "Knee X-Ray", ServiceStatus.PUBLISHED
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

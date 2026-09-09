"""POST /services/{id}/publish and GET /services/{id}/publish-status
through the real FastAPI routing layer.

publish_service calls the real Temporal client -- these tests replace it
with a fake (monkeypatched where service_publish.py uses it, not where
get_temporal_client is defined) so they prove the endpoint's own logic
(role, guard, status transition, response shape), not Temporal
connectivity. The real workflow is already proven by
test_publish_workflow.py and the live worker check.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Department, Service
from app.models.enums import ServiceStatus


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


class _FakeTemporalClient:
    """Records what it was asked to start, and does nothing else."""

    def __init__(self) -> None:
        self.started_with: list[tuple[Any, ...]] = []

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        self.started_with.append((args, kwargs))


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

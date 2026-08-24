"""Service routes through the real FastAPI routing layer -- the
require_role gates on the staff CRUD, the public /search route sitting
right next to them with no gate at all, and the request-body property
Day 4 proved live with curl: status is never writable through this
surface, only Week 2's publish workflow can move a service off DRAFT.
"""

from sqlalchemy.orm import Session

from fastapi.testclient import TestClient

from app.models import Department, Service
from app.models.enums import ServiceStatus


def _make_service(
    db_session: Session, department: Department, name: str, status: ServiceStatus = ServiceStatus.PUBLISHED
) -> Service:
    s = Service(department_id=department.id, name=name, status=status)
    db_session.add(s)
    db_session.flush()
    return s


def test_create_service_returns_201_and_ignores_a_status_field(
    client: TestClient, department: Department, admin_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/services",
        json={
            "department_id": department.id,
            "name": "Knee X-Ray",
            "status": "PUBLISHED",  # not a real field on ServiceCreate
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Knee X-Ray"
    assert body["status"] == "DRAFT"
    assert body["published_at"] is None


def test_create_service_forbidden_for_non_admin_staff(
    client: TestClient, department: Department, front_desk_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/services",
        json={"department_id": department.id, "name": "Knee X-Ray"},
        headers=front_desk_auth_headers,
    )

    assert response.status_code == 403


def test_create_service_duplicate_name_returns_409(
    client: TestClient, db_session: Session, department: Department, admin_auth_headers: dict
) -> None:
    existing = _make_service(db_session, department, "Knee X-Ray", ServiceStatus.DRAFT)

    response = client.post(
        "/api/v1/services",
        json={"department_id": department.id, "name": existing.name},
        headers=admin_auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SERVICE_NAME_TAKEN"


def test_create_service_unknown_department_returns_404(
    client: TestClient, admin_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/services",
        json={"department_id": 999999, "name": "Knee X-Ray"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPARTMENT_NOT_FOUND"


def test_list_services_returns_items_for_staff(
    client: TestClient, db_session: Session, department: Department, provider_auth_headers: dict
) -> None:
    service = _make_service(db_session, department, "Knee X-Ray", ServiceStatus.DRAFT)

    response = client.get("/api/v1/services", headers=provider_auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert any(item["id"] == service.id for item in body["items"])


def test_list_services_forbidden_for_patient(
    client: TestClient, patient_auth_headers: dict
) -> None:
    response = client.get("/api/v1/services", headers=patient_auth_headers)

    assert response.status_code == 403


def test_get_service_returns_200(
    client: TestClient, db_session: Session, department: Department, provider_auth_headers: dict
) -> None:
    service = _make_service(db_session, department, "Knee X-Ray", ServiceStatus.DRAFT)

    response = client.get(f"/api/v1/services/{service.id}", headers=provider_auth_headers)

    assert response.status_code == 200
    assert response.json()["id"] == service.id


def test_get_service_not_found_returns_404(
    client: TestClient, provider_auth_headers: dict
) -> None:
    response = client.get("/api/v1/services/999999", headers=provider_auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SERVICE_NOT_FOUND"


def test_get_service_without_a_header_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/services/1")

    assert response.status_code == 401


def test_update_service_returns_200_and_ignores_a_status_field(
    client: TestClient, db_session: Session, department: Department, admin_auth_headers: dict
) -> None:
    service = _make_service(db_session, department, "Knee X-Ray", ServiceStatus.DRAFT)

    response = client.patch(
        f"/api/v1/services/{service.id}",
        json={"description": "Standard knee imaging.", "status": "PUBLISHED"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "Standard knee imaging."
    assert body["status"] == "DRAFT"


def test_update_service_forbidden_for_non_admin_staff(
    client: TestClient, db_session: Session, department: Department, provider_auth_headers: dict
) -> None:
    service = _make_service(db_session, department, "Knee X-Ray", ServiceStatus.DRAFT)

    response = client.patch(
        f"/api/v1/services/{service.id}",
        json={"description": "Standard knee imaging."},
        headers=provider_auth_headers,
    )

    assert response.status_code == 403


def test_update_service_duplicate_name_returns_409(
    client: TestClient, db_session: Session, department: Department, admin_auth_headers: dict
) -> None:
    _make_service(db_session, department, "Knee X-Ray", ServiceStatus.DRAFT)
    other = _make_service(db_session, department, "MRI Scan", ServiceStatus.DRAFT)

    response = client.patch(
        f"/api/v1/services/{other.id}",
        json={"name": "Knee X-Ray"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SERVICE_NAME_TAKEN"


def test_search_services_succeeds_with_no_auth_header_and_excludes_draft(
    client: TestClient, db_session: Session, department: Department
) -> None:
    _make_service(db_session, department, "Knee X-Ray", ServiceStatus.DRAFT)
    published = _make_service(db_session, department, "MRI Scan", ServiceStatus.PUBLISHED)

    response = client.get("/api/v1/services/search")

    assert response.status_code == 200
    body = response.json()
    ids = [item["id"] for item in body["items"]]
    assert published.id in ids
    assert all(item["id"] != published.id or item["name"] == "MRI Scan" for item in body["items"])

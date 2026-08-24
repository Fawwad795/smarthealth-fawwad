"""Department routes through the real FastAPI routing layer -- the
require_role gates (admin-only write, any-staff-role read), and the 404/409
paths services/department.py raises.
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Clinic, Department


def test_create_department_returns_201_for_admin(
    client: TestClient, clinic: Clinic, admin_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/departments",
        json={"clinic_id": clinic.id, "name": "Neurology", "order_index": 2},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Neurology"
    assert body["clinic_id"] == clinic.id


def test_create_department_forbidden_for_non_admin_staff(
    client: TestClient, clinic: Clinic, front_desk_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/departments",
        json={"clinic_id": clinic.id, "name": "Neurology"},
        headers=front_desk_auth_headers,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_create_department_duplicate_name_returns_409(
    client: TestClient, department: Department, admin_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/departments",
        json={"clinic_id": department.clinic_id, "name": department.name},
        headers=admin_auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DEPARTMENT_NAME_TAKEN"


def test_create_department_unknown_clinic_returns_404(
    client: TestClient, admin_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/departments",
        json={"clinic_id": 999999, "name": "Neurology"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CLINIC_NOT_FOUND"


def test_list_departments_returns_items_for_any_staff_role(
    client: TestClient, department: Department, provider_auth_headers: dict
) -> None:
    response = client.get("/api/v1/departments", headers=provider_auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert any(item["id"] == department.id for item in body["items"])


def test_list_departments_forbidden_for_patient(
    client: TestClient, patient_auth_headers: dict
) -> None:
    response = client.get("/api/v1/departments", headers=patient_auth_headers)

    assert response.status_code == 403


def test_list_departments_without_a_header_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/departments")

    assert response.status_code == 401


def test_get_department_returns_200(
    client: TestClient, department: Department, provider_auth_headers: dict
) -> None:
    response = client.get(
        f"/api/v1/departments/{department.id}", headers=provider_auth_headers
    )

    assert response.status_code == 200
    assert response.json()["id"] == department.id


def test_get_department_not_found_returns_404(
    client: TestClient, provider_auth_headers: dict
) -> None:
    response = client.get("/api/v1/departments/999999", headers=provider_auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPARTMENT_NOT_FOUND"


def test_update_department_returns_200_for_admin(
    client: TestClient, department: Department, admin_auth_headers: dict
) -> None:
    response = client.patch(
        f"/api/v1/departments/{department.id}",
        json={"order_index": 5},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["order_index"] == 5


def test_update_department_forbidden_for_non_admin_staff(
    client: TestClient, department: Department, provider_auth_headers: dict
) -> None:
    response = client.patch(
        f"/api/v1/departments/{department.id}",
        json={"order_index": 5},
        headers=provider_auth_headers,
    )

    assert response.status_code == 403


def test_update_department_duplicate_name_returns_409(
    client: TestClient,
    db_session: Session,
    department: Department,
    admin_auth_headers: dict,
) -> None:
    other = Department(clinic_id=department.clinic_id, name="Radiology", order_index=9)
    db_session.add(other)
    db_session.flush()

    response = client.patch(
        f"/api/v1/departments/{other.id}",
        json={"name": department.name},
        headers=admin_auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DEPARTMENT_NAME_TAKEN"

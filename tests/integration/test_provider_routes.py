"""Provider routes through the real FastAPI routing layer -- the
require_role gates, and create_provider's three distinct rejections
(missing user, wrong role, already has a profile), each of which must
reach the client as its own status code.
"""

from fastapi.testclient import TestClient

from app.models import Department, Provider, Specialty, User


def test_create_provider_returns_201_for_admin(
    client: TestClient,
    provider_user: User,
    department: Department,
    specialty: Specialty,
    admin_auth_headers: dict,
) -> None:
    response = client.post(
        "/api/v1/providers",
        json={
            "user_id": provider_user.id,
            "department_id": department.id,
            "specialty_id": specialty.id,
            "bio": "Consultant cardiologist.",
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] == provider_user.id


def test_create_provider_forbidden_for_non_admin_staff(
    client: TestClient,
    provider_user: User,
    department: Department,
    specialty: Specialty,
    front_desk_auth_headers: dict,
) -> None:
    response = client.post(
        "/api/v1/providers",
        json={
            "user_id": provider_user.id,
            "department_id": department.id,
            "specialty_id": specialty.id,
        },
        headers=front_desk_auth_headers,
    )

    assert response.status_code == 403


def test_create_provider_unknown_user_returns_404(
    client: TestClient,
    department: Department,
    specialty: Specialty,
    admin_auth_headers: dict,
) -> None:
    response = client.post(
        "/api/v1/providers",
        json={
            "user_id": 999999,
            "department_id": department.id,
            "specialty_id": specialty.id,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_create_provider_wrong_role_returns_409(
    client: TestClient,
    patient_user: User,
    department: Department,
    specialty: Specialty,
    admin_auth_headers: dict,
) -> None:
    response = client.post(
        "/api/v1/providers",
        json={
            "user_id": patient_user.id,
            "department_id": department.id,
            "specialty_id": specialty.id,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "USER_NOT_A_PROVIDER"


def test_create_provider_duplicate_profile_returns_409(
    client: TestClient, provider: Provider, admin_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/providers",
        json={
            "user_id": provider.user_id,
            "department_id": provider.department_id,
            "specialty_id": provider.specialty_id,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROVIDER_PROFILE_EXISTS"


def test_list_providers_returns_items_for_staff(
    client: TestClient, provider: Provider, provider_auth_headers: dict
) -> None:
    response = client.get("/api/v1/providers", headers=provider_auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert any(item["id"] == provider.id for item in body["items"])


def test_list_providers_forbidden_for_patient(
    client: TestClient, patient_auth_headers: dict
) -> None:
    response = client.get("/api/v1/providers", headers=patient_auth_headers)

    assert response.status_code == 403


def test_get_provider_returns_200(
    client: TestClient, provider: Provider, provider_auth_headers: dict
) -> None:
    response = client.get(
        f"/api/v1/providers/{provider.id}", headers=provider_auth_headers
    )

    assert response.status_code == 200
    assert response.json()["id"] == provider.id


def test_get_provider_not_found_returns_404(
    client: TestClient, provider_auth_headers: dict
) -> None:
    response = client.get("/api/v1/providers/999999", headers=provider_auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROVIDER_NOT_FOUND"


def test_update_provider_returns_200_for_admin(
    client: TestClient, provider: Provider, admin_auth_headers: dict
) -> None:
    response = client.patch(
        f"/api/v1/providers/{provider.id}",
        json={"bio": "Senior consultant cardiologist."},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["bio"] == "Senior consultant cardiologist."


def test_update_provider_forbidden_for_non_admin_staff(
    client: TestClient, provider: Provider, provider_auth_headers: dict
) -> None:
    response = client.patch(
        f"/api/v1/providers/{provider.id}",
        json={"bio": "Senior consultant cardiologist."},
        headers=provider_auth_headers,
    )

    assert response.status_code == 403


def test_update_provider_unknown_department_returns_404(
    client: TestClient, provider: Provider, admin_auth_headers: dict
) -> None:
    response = client.patch(
        f"/api/v1/providers/{provider.id}",
        json={"department_id": 999999},
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEPARTMENT_OR_SPECIALTY_NOT_FOUND"

"""Auth routes through the real FastAPI routing layer -- status codes, the
JSON error envelope, and get_current_user actually parsing a header. The
business rules themselves (duplicate email, wrong password, deactivated
account) are already proven against services/auth.py directly in
test_auth_service.py; this file only proves the router wires them correctly.
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Patient, User


def test_register_returns_201_and_creates_a_patient_row(
    client: TestClient, db_session: Session
) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "ayesha@example.com",
            "password": "correct horse battery staple",
            "dob": "1990-05-14",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "ayesha@example.com"
    assert "password" not in body and "password_hash" not in body

    user = db_session.query(User).filter(User.id == body["id"]).one()
    patient = db_session.query(Patient).filter(Patient.user_id == user.id).one()
    assert str(patient.dob) == "1990-05-14"


def test_register_duplicate_email_returns_409(client: TestClient) -> None:
    payload = {
        "email": "ayesha@example.com",
        "password": "correct horse battery staple",
        "dob": "1990-05-14",
    }
    client.post("/api/v1/auth/register", json=payload)

    response = client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_TAKEN"


def test_register_rejects_a_too_short_password_with_422(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "ayesha@example.com", "password": "short", "dob": "1990-05-14"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_login_token_authenticates_a_later_me_call(client: TestClient) -> None:
    payload = {
        "email": "ayesha@example.com",
        "password": "correct horse battery staple",
        "dob": "1990-05-14",
    }
    client.post("/api/v1/auth/register", json=payload)

    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
    )
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]

    me_response = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me_response.status_code == 200
    assert me_response.json()["email"] == payload["email"]


def test_login_wrong_password_returns_401(client: TestClient) -> None:
    payload = {
        "email": "ayesha@example.com",
        "password": "correct horse battery staple",
        "dob": "1990-05-14",
    }
    client.post("/api/v1/auth/register", json=payload)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": "wrong password"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_login_unknown_email_returns_401(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "whatever123"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_me_without_a_header_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_me_with_a_garbage_token_returns_401(client: TestClient) -> None:
    response = client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )

    assert response.status_code == 401


def test_me_returns_the_authenticated_users_own_role(
    client: TestClient, provider_auth_headers: dict, provider_user: User
) -> None:
    response = client.get("/api/v1/auth/me", headers=provider_auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == provider_user.id
    assert body["role"] == "PROVIDER"

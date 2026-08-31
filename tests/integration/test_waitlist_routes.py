"""POST /waitlist through the real FastAPI routing layer."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Patient, Provider, User, Waitlist
from app.models.enums import UserRole, WaitlistStatus


def test_patient_joins_a_providers_waitlist(
    client: TestClient,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
) -> None:
    response = client.post(
        "/api/v1/waitlist",
        json={"provider_id": provider.id},
        headers=patient_auth_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["provider_id"] == provider.id
    assert body["patient_id"] == patient.id
    assert body["status"] == "WAITING"


def test_joining_the_same_queue_twice_returns_409(
    client: TestClient,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
) -> None:
    payload = {"provider_id": provider.id}
    client.post("/api/v1/waitlist", json=payload, headers=patient_auth_headers)

    response = client.post(
        "/api/v1/waitlist", json=payload, headers=patient_auth_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ALREADY_ON_WAITLIST"


def test_front_desk_can_join_on_a_patients_behalf(
    client: TestClient,
    patient: Patient,
    front_desk_auth_headers: dict,
    provider: Provider,
) -> None:
    response = client.post(
        "/api/v1/waitlist",
        json={"provider_id": provider.id, "patient_id": patient.id},
        headers=front_desk_auth_headers,
    )

    assert response.status_code == 201
    assert response.json()["patient_id"] == patient.id


def test_front_desk_without_patient_id_returns_400(
    client: TestClient, front_desk_auth_headers: dict, provider: Provider
) -> None:
    response = client.post(
        "/api/v1/waitlist",
        json={"provider_id": provider.id},
        headers=front_desk_auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PATIENT_ID_REQUIRED"


def test_unknown_provider_returns_404(
    client: TestClient, patient: Patient, patient_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/waitlist",
        json={"provider_id": 999999},
        headers=patient_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROVIDER_NOT_FOUND"


def test_patient_cannot_join_in_someone_elses_name(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
) -> None:
    other_user = User(
        email="waitlist-other@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(other_user)
    db_session.flush()
    other_patient = Patient(user_id=other_user.id, dob=patient.dob)
    db_session.add(other_patient)
    db_session.flush()

    response = client.post(
        "/api/v1/waitlist",
        json={"provider_id": provider.id, "patient_id": other_patient.id},
        headers=patient_auth_headers,
    )

    assert response.status_code == 201
    assert response.json()["patient_id"] == patient.id


def test_provider_role_cannot_join_a_waitlist(
    client: TestClient, provider_auth_headers: dict, provider: Provider
) -> None:
    response = client.post(
        "/api/v1/waitlist",
        json={"provider_id": provider.id},
        headers=provider_auth_headers,
    )

    assert response.status_code == 403


def test_two_patients_can_wait_for_the_same_provider_oldest_first(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    front_desk_auth_headers: dict,
    provider: Provider,
) -> None:
    """uq_waitlist_one_waiting_entry constrains (provider, patient) pairs,
    not the provider alone. If it were ever narrowed to provider_id, only
    one person could ever wait for a given doctor -- and every route test
    above would still pass. The (created_at, id) order is what the next
    subtask's promotion reads "next in line" from.
    """
    second_user = User(
        email="waitlist-second@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(second_user)
    db_session.flush()
    second_patient = Patient(user_id=second_user.id, dob=patient.dob)
    db_session.add(second_patient)
    db_session.flush()

    first = client.post(
        "/api/v1/waitlist",
        json={"provider_id": provider.id},
        headers=patient_auth_headers,
    )
    second = client.post(
        "/api/v1/waitlist",
        json={"provider_id": provider.id, "patient_id": second_patient.id},
        headers=front_desk_auth_headers,
    )

    assert first.status_code == 201
    assert second.status_code == 201

    queue = (
        db_session.query(Waitlist)
        .filter(Waitlist.provider_id == provider.id)
        .order_by(Waitlist.created_at, Waitlist.id)
        .all()
    )
    assert [e.patient_id for e in queue] == [patient.id, second_patient.id]
    assert all(e.status == WaitlistStatus.WAITING for e in queue)

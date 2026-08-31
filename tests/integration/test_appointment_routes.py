"""POST /appointments and GET /appointments/{id} through the real FastAPI
routing layer.

request_appointment calls the real Temporal client -- these tests replace
it with a fake (monkeypatched where appointment_scheduling.py uses it),
same pattern as test_service_publish_routes.py, so they prove the
endpoint's own logic (role, patient resolution, idempotency, response
shape), not Temporal connectivity. The real saga is already proven by
test_scheduling_workflow.py.

Every test uses its own Idempotency-Key. The test Redis database is
flushed once per session, not per test, so a key reused across two tests
would be a cache hit pointing at an appointment the first test's
transaction already rolled back.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models import Appointment, Patient, Provider, Service, Slot, User
from app.models.enums import UserRole


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
        "app.services.appointment_scheduling.get_temporal_client",
        _fake_get_temporal_client,
    )
    return fake_client


def _booking_payload(provider: Provider, slot: Slot, service: Service) -> dict:
    return {
        "provider_id": provider.id,
        "slot_id": slot.id,
        "service_id": service.id,
    }


def test_create_appointment_patient_books_for_self_returns_202(
    client: TestClient,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    slot: Slot,
    service: Service,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    response = client.post(
        "/api/v1/appointments",
        json=_booking_payload(provider, slot, service),
        headers={**patient_auth_headers, "Idempotency-Key": "key-self-book"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["patient_id"] == patient.id
    assert body["status"] == "REQUESTED"
    assert body["workflow_id"] == f"schedule-appointment-{body['id']}"
    assert len(fake_temporal_client.started_with) == 1


def test_create_appointment_ignores_patient_id_sent_by_a_patient(
    client: TestClient,
    db_session: Session,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    slot: Slot,
    service: Service,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    # A patient cannot book in someone else's name by setting patient_id --
    # resolve_booking_patient derives it from the token, never the body.
    other_user = User(
        email="someone-else@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(other_user)
    db_session.flush()
    other_patient = Patient(user_id=other_user.id, dob=patient.dob)
    db_session.add(other_patient)
    db_session.flush()

    payload = _booking_payload(provider, slot, service)
    payload["patient_id"] = other_patient.id

    response = client.post(
        "/api/v1/appointments",
        json=payload,
        headers={**patient_auth_headers, "Idempotency-Key": "key-spoof-attempt"},
    )

    assert response.status_code == 202
    assert response.json()["patient_id"] == patient.id


def test_create_appointment_repeated_idempotency_key_returns_original(
    client: TestClient,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    slot: Slot,
    service: Service,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    headers = {**patient_auth_headers, "Idempotency-Key": "key-repeat"}
    payload = _booking_payload(provider, slot, service)

    first = client.post("/api/v1/appointments", json=payload, headers=headers)
    second = client.post("/api/v1/appointments", json=payload, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    # The whole point: one saga, not two.
    assert len(fake_temporal_client.started_with) == 1


def test_create_appointment_front_desk_can_book_for_a_patient(
    client: TestClient,
    patient: Patient,
    front_desk_auth_headers: dict,
    provider: Provider,
    slot: Slot,
    service: Service,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    payload = _booking_payload(provider, slot, service)
    payload["patient_id"] = patient.id

    response = client.post(
        "/api/v1/appointments",
        json=payload,
        headers={**front_desk_auth_headers, "Idempotency-Key": "key-front-desk"},
    )

    assert response.status_code == 202
    assert response.json()["patient_id"] == patient.id


def test_create_appointment_front_desk_without_patient_id_returns_400(
    client: TestClient,
    front_desk_auth_headers: dict,
    provider: Provider,
    slot: Slot,
    service: Service,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    response = client.post(
        "/api/v1/appointments",
        json=_booking_payload(provider, slot, service),
        headers={**front_desk_auth_headers, "Idempotency-Key": "key-missing-patient"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PATIENT_ID_REQUIRED"
    assert fake_temporal_client.started_with == []


def test_create_appointment_unknown_provider_returns_404(
    client: TestClient,
    patient: Patient,
    patient_auth_headers: dict,
    slot: Slot,
    service: Service,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    payload = {"provider_id": 999999, "slot_id": slot.id, "service_id": service.id}

    response = client.post(
        "/api/v1/appointments",
        json=payload,
        headers={**patient_auth_headers, "Idempotency-Key": "key-bad-provider"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROVIDER_NOT_FOUND"
    assert fake_temporal_client.started_with == []


def test_create_appointment_without_idempotency_key_is_rejected(
    client: TestClient,
    patient: Patient,
    patient_auth_headers: dict,
    provider: Provider,
    slot: Slot,
    service: Service,
    fake_temporal_client: _FakeTemporalClient,
) -> None:
    response = client.post(
        "/api/v1/appointments",
        json=_booking_payload(provider, slot, service),
        headers=patient_auth_headers,
    )

    assert response.status_code == 422
    assert fake_temporal_client.started_with == []


def test_get_appointment_state_returns_current_status(
    client: TestClient, appointment: Appointment, patient_auth_headers: dict
) -> None:
    response = client.get(
        f"/api/v1/appointments/{appointment.id}", headers=patient_auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == appointment.id
    assert body["status"] == "REQUESTED"
    assert body["workflow_id"] == f"schedule-appointment-{appointment.id}"


def test_get_appointment_state_forbidden_for_a_different_patient(
    client: TestClient, db_session: Session, appointment: Appointment
) -> None:
    other_user = User(
        email="other-patient@example.com",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db_session.add(other_user)
    db_session.flush()
    other_headers = {"Authorization": f"Bearer {create_access_token(other_user.id)}"}

    response = client.get(
        f"/api/v1/appointments/{appointment.id}", headers=other_headers
    )

    assert response.status_code == 403


def test_get_appointment_state_staff_can_see_any_patients_appointment(
    client: TestClient, appointment: Appointment, front_desk_auth_headers: dict
) -> None:
    response = client.get(
        f"/api/v1/appointments/{appointment.id}", headers=front_desk_auth_headers
    )

    assert response.status_code == 200
    assert response.json()["id"] == appointment.id


def test_get_appointment_state_not_found_returns_404(
    client: TestClient, patient_auth_headers: dict
) -> None:
    response = client.get("/api/v1/appointments/999999", headers=patient_auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "APPOINTMENT_NOT_FOUND"

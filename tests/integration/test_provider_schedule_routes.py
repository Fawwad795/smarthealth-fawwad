"""Provider schedule routes through the real FastAPI routing layer -- the
require_role gates, the nested-resource ownership check (a schedule that
belongs to a different provider must 404, not just a nonexistent one),
and generate-slots' idempotency, the property the whole week built
toward.
"""

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Department, Provider, Specialty, User
from app.models.enums import UserRole
from app.schemas.provider_schedule import ProviderScheduleCreate
from app.services import provider_schedule as schedule_service


def _make_other_provider(db_session: Session, department: Department, specialty: Specialty) -> Provider:
    user = User(email="dr.other@example.com", password_hash="not-a-real-hash", role=UserRole.PROVIDER)
    db_session.add(user)
    db_session.flush()
    other = Provider(user_id=user.id, department_id=department.id, specialty_id=specialty.id)
    db_session.add(other)
    db_session.flush()
    return other


def test_create_schedule_returns_201_for_admin(
    client: TestClient, provider: Provider, admin_auth_headers: dict
) -> None:
    response = client.post(
        f"/api/v1/providers/{provider.id}/schedules",
        json={"weekday": 0, "start_time": "09:00:00", "end_time": "17:00:00"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    assert response.json()["provider_id"] == provider.id


def test_create_schedule_forbidden_for_non_admin_staff(
    client: TestClient, provider: Provider, provider_auth_headers: dict
) -> None:
    response = client.post(
        f"/api/v1/providers/{provider.id}/schedules",
        json={"weekday": 0, "start_time": "09:00:00", "end_time": "17:00:00"},
        headers=provider_auth_headers,
    )

    assert response.status_code == 403


def test_create_schedule_unknown_provider_returns_404(
    client: TestClient, admin_auth_headers: dict
) -> None:
    response = client.post(
        "/api/v1/providers/999999/schedules",
        json={"weekday": 0, "start_time": "09:00:00", "end_time": "17:00:00"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROVIDER_NOT_FOUND"


def test_create_schedule_end_before_start_returns_422(
    client: TestClient, provider: Provider, admin_auth_headers: dict
) -> None:
    response = client.post(
        f"/api/v1/providers/{provider.id}/schedules",
        json={"weekday": 0, "start_time": "17:00:00", "end_time": "09:00:00"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_schedule_duplicate_window_returns_409(
    client: TestClient, db_session: Session, provider: Provider, admin_auth_headers: dict
) -> None:
    schedule_service.create_provider_schedule(
        db_session, provider.id, ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="17:00")
    )

    response = client.post(
        f"/api/v1/providers/{provider.id}/schedules",
        json={"weekday": 0, "start_time": "09:00:00", "end_time": "12:00:00"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DUPLICATE_SCHEDULE_WINDOW"


def test_list_schedules_returns_items_for_staff(
    client: TestClient, db_session: Session, provider: Provider, provider_auth_headers: dict
) -> None:
    schedule = schedule_service.create_provider_schedule(
        db_session, provider.id, ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="17:00")
    )

    response = client.get(f"/api/v1/providers/{provider.id}/schedules", headers=provider_auth_headers)

    assert response.status_code == 200
    assert any(item["id"] == schedule.id for item in response.json()["items"])


def test_list_schedules_forbidden_for_patient(
    client: TestClient, provider: Provider, patient_auth_headers: dict
) -> None:
    response = client.get(f"/api/v1/providers/{provider.id}/schedules", headers=patient_auth_headers)

    assert response.status_code == 403


def test_get_schedule_returns_200(
    client: TestClient, db_session: Session, provider: Provider, provider_auth_headers: dict
) -> None:
    schedule = schedule_service.create_provider_schedule(
        db_session, provider.id, ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="17:00")
    )

    response = client.get(
        f"/api/v1/providers/{provider.id}/schedules/{schedule.id}", headers=provider_auth_headers
    )

    assert response.status_code == 200
    assert response.json()["id"] == schedule.id


def test_get_schedule_not_found_returns_404(
    client: TestClient, provider: Provider, provider_auth_headers: dict
) -> None:
    response = client.get(
        f"/api/v1/providers/{provider.id}/schedules/999999", headers=provider_auth_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCHEDULE_NOT_FOUND"


def test_get_schedule_belonging_to_a_different_provider_returns_404(
    client: TestClient,
    db_session: Session,
    provider: Provider,
    department: Department,
    specialty: Specialty,
    provider_auth_headers: dict,
) -> None:
    other_provider = _make_other_provider(db_session, department, specialty)
    schedule = schedule_service.create_provider_schedule(
        db_session, provider.id, ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="17:00")
    )

    response = client.get(
        f"/api/v1/providers/{other_provider.id}/schedules/{schedule.id}",
        headers=provider_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCHEDULE_NOT_FOUND"


def test_update_schedule_returns_200_for_admin(
    client: TestClient, db_session: Session, provider: Provider, admin_auth_headers: dict
) -> None:
    schedule = schedule_service.create_provider_schedule(
        db_session, provider.id, ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="17:00")
    )

    response = client.patch(
        f"/api/v1/providers/{provider.id}/schedules/{schedule.id}",
        json={"is_active": False},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False


def test_update_schedule_forbidden_for_non_admin_staff(
    client: TestClient, db_session: Session, provider: Provider, provider_auth_headers: dict
) -> None:
    schedule = schedule_service.create_provider_schedule(
        db_session, provider.id, ProviderScheduleCreate(weekday=0, start_time="09:00", end_time="17:00")
    )

    response = client.patch(
        f"/api/v1/providers/{provider.id}/schedules/{schedule.id}",
        json={"is_active": False},
        headers=provider_auth_headers,
    )

    assert response.status_code == 403


def test_generate_slots_creates_then_is_idempotent_on_a_second_call(
    client: TestClient, db_session: Session, provider: Provider, admin_auth_headers: dict
) -> None:
    target_date = date(2026, 8, 24)
    schedule_service.create_provider_schedule(
        db_session,
        provider.id,
        ProviderScheduleCreate(
            weekday=target_date.weekday(), start_time="09:00", end_time="10:00", slot_duration_minutes=30
        ),
    )
    payload = {"start_date": str(target_date), "end_date": str(target_date)}

    first = client.post(
        f"/api/v1/providers/{provider.id}/schedules/generate-slots",
        json=payload,
        headers=admin_auth_headers,
    )
    assert first.status_code == 200
    assert first.json() == {"created": 2, "skipped": 0}

    second = client.post(
        f"/api/v1/providers/{provider.id}/schedules/generate-slots",
        json=payload,
        headers=admin_auth_headers,
    )
    assert second.json() == {"created": 0, "skipped": 2}


def test_generate_slots_forbidden_for_non_admin_staff(
    client: TestClient, provider: Provider, provider_auth_headers: dict
) -> None:
    response = client.post(
        f"/api/v1/providers/{provider.id}/schedules/generate-slots",
        json={"start_date": "2026-08-24", "end_date": "2026-08-24"},
        headers=provider_auth_headers,
    )

    assert response.status_code == 403

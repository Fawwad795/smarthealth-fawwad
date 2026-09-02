"""Provider service: create/list/get/update against a real database.

The behaviour worth pinning down here, beyond CRUD: creating a provider
profile requires a user_id that already exists and already has role
PROVIDER -- there is no way to become a provider through this endpoint,
only to attach an operational profile to an account provisioned elsewhere
(the seed script, task 1.10).
"""

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.pagination import PaginationParams
from app.models import Clinic, Department, Provider, Specialty, User
from app.models.enums import UserRole
from app.schemas.provider import ProviderCreate, ProviderUpdate
from app.services import provider as provider_service


def test_create_provider_persists_it(
    db_session: Session,
    provider_user: User,
    department: Department,
    specialty: Specialty,
) -> None:
    data = ProviderCreate(
        user_id=provider_user.id,
        department_id=department.id,
        specialty_id=specialty.id,
        bio="Consultant cardiologist.",
    )

    provider = provider_service.create_provider(db_session, data)

    assert provider.id is not None
    assert provider.user_id == provider_user.id
    assert provider.bio == "Consultant cardiologist."


def test_create_provider_rejects_a_non_provider_user(
    db_session: Session,
    patient_user: User,
    department: Department,
    specialty: Specialty,
) -> None:
    data = ProviderCreate(
        user_id=patient_user.id, department_id=department.id, specialty_id=specialty.id
    )

    with pytest.raises(AppError) as exc_info:
        provider_service.create_provider(db_session, data)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "USER_NOT_A_PROVIDER"


def test_create_provider_rejects_a_missing_user(
    db_session: Session, department: Department, specialty: Specialty
) -> None:
    data = ProviderCreate(
        user_id=999999999, department_id=department.id, specialty_id=specialty.id
    )

    with pytest.raises(AppError) as exc_info:
        provider_service.create_provider(db_session, data)
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "USER_NOT_FOUND"


def test_create_provider_rejects_a_user_who_already_has_a_profile(
    db_session: Session,
    provider: Provider,
    department: Department,
    specialty: Specialty,
) -> None:
    data = ProviderCreate(
        user_id=provider.user_id, department_id=department.id, specialty_id=specialty.id
    )

    with pytest.raises(AppError) as exc_info:
        provider_service.create_provider(db_session, data)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "PROVIDER_PROFILE_EXISTS"


def test_get_provider_raises_404_when_missing(db_session: Session) -> None:
    with pytest.raises(AppError) as exc_info:
        provider_service.get_provider(db_session, 999999999)
    assert exc_info.value.status_code == 404


def test_update_provider_changes_department_and_bio(
    db_session: Session, provider: Provider, clinic: Clinic
) -> None:
    other_department = Department(clinic_id=clinic.id, name="Neurology", order_index=1)
    db_session.add(other_department)
    db_session.flush()

    updated = provider_service.update_provider(
        db_session,
        provider.id,
        ProviderUpdate(department_id=other_department.id, bio="Now in Neurology."),
    )

    assert updated.department_id == other_department.id
    assert updated.bio == "Now in Neurology."


def test_list_providers_paginates(
    db_session: Session, department: Department, specialty: Specialty
) -> None:
    for i in range(3):
        user = User(
            email=f"provider{i}@example.com",
            password_hash="not-a-real-hash",
            role=UserRole.PROVIDER,
        )
        db_session.add(user)
        db_session.flush()
        provider_service.create_provider(
            db_session,
            ProviderCreate(
                user_id=user.id, department_id=department.id, specialty_id=specialty.id
            ),
        )

    items, total = provider_service.list_providers(
        db_session, PaginationParams(limit=2, offset=0)
    )
    assert total == 3
    assert len(items) == 2

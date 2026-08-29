"""Role-based authorization and patient-data ownership -- three distinct
checks (Section 5.2), tested independently of any endpoint. No database:
these functions only ever look at already-loaded objects.
"""

import pytest

from app.core.dependencies import ensure_patient_self_or_staff, require_role
from app.core.exceptions import AppError
from app.models import Patient, User
from app.models.enums import UserRole


def _user(id: int, role: UserRole) -> User:
    return User(id=id, email=f"user{id}@example.com", password_hash="x", role=role)


def _patient(user_id: int) -> Patient:
    return Patient(user_id=user_id, dob="1990-01-01")


def test_require_role_allows_a_matching_role() -> None:
    check = require_role(UserRole.PROVIDER)
    provider = _user(1, UserRole.PROVIDER)
    assert check(current_user=provider) is provider


def test_require_role_rejects_a_non_matching_role() -> None:
    check = require_role(UserRole.PROVIDER)
    patient = _user(2, UserRole.PATIENT)
    with pytest.raises(AppError) as exc_info:
        check(current_user=patient)
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "FORBIDDEN"


def test_require_role_accepts_any_of_several_roles() -> None:
    check = require_role(UserRole.FRONT_DESK, UserRole.ADMIN)
    admin = _user(3, UserRole.ADMIN)
    assert check(current_user=admin) is admin


def test_patient_can_read_their_own_data() -> None:
    patient_user = _user(2, UserRole.PATIENT)
    own_record = _patient(user_id=2)
    ensure_patient_self_or_staff(patient_user, own_record)  # no exception


def test_patient_cannot_read_another_patients_data() -> None:
    patient_user = _user(2, UserRole.PATIENT)
    someone_elses_record = _patient(user_id=99)
    with pytest.raises(AppError) as exc_info:
        ensure_patient_self_or_staff(patient_user, someone_elses_record)
    assert exc_info.value.status_code == 403


def test_staff_can_read_any_patients_data() -> None:
    someone_elses_record = _patient(user_id=99)
    for role in (UserRole.FRONT_DESK, UserRole.ADMIN):
        ensure_patient_self_or_staff(
            _user(5, role), someone_elses_record
        )  # no exception


def test_a_provider_is_not_automatically_staff() -> None:
    """A provider is not front_desk/admin and is not the patient -- this
    check has no concept yet of "is this one of my patients" (that needs
    an appointment relationship, which doesn't exist until Week 2), so for
    now a provider is correctly rejected here, same as any stranger.
    """
    provider = _user(1, UserRole.PROVIDER)
    someone_elses_record = _patient(user_id=99)
    with pytest.raises(AppError):
        ensure_patient_self_or_staff(provider, someone_elses_record)

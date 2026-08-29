"""services/auth.py against a real database -- uniqueness and credential
checks that only mean something with an actual users table behind them.
"""

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.security import decode_access_token, verify_password
from app.models import Patient
from app.models.enums import UserRole
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.auth import login, register_patient


def test_register_patient_creates_a_user_and_patient_row(db_session: Session) -> None:
    data = RegisterRequest(
        email="Ayesha@Example.com", password="correct horse battery staple", dob="1990-05-14"
    )
    user = register_patient(db_session, data)

    assert user.email == "ayesha@example.com"  # lowercased on write
    assert user.role == UserRole.PATIENT
    assert verify_password("correct horse battery staple", user.password_hash)

    patient = db_session.query(Patient).filter(Patient.user_id == user.id).one()
    assert str(patient.dob) == "1990-05-14"


def test_register_patient_rejects_a_duplicate_email_case_insensitively(
    db_session: Session,
) -> None:
    register_patient(
        db_session,
        RegisterRequest(email="ayesha@example.com", password="a real password", dob="1990-05-14"),
    )
    with pytest.raises(AppError) as exc_info:
        register_patient(
            db_session,
            RegisterRequest(
                email="AYESHA@EXAMPLE.COM", password="a different password", dob="1990-05-14"
            ),
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "EMAIL_TAKEN"


def test_login_returns_a_token_for_the_right_user(db_session: Session) -> None:
    user = register_patient(
        db_session,
        RegisterRequest(email="ayesha@example.com", password="correct horse battery staple", dob="1990-05-14"),
    )
    token = login(
        db_session,
        LoginRequest(email="ayesha@example.com", password="correct horse battery staple"),
    )
    assert decode_access_token(token) == user.id


def test_login_rejects_wrong_password_and_missing_email_identically(
    db_session: Session,
) -> None:
    register_patient(
        db_session,
        RegisterRequest(email="ayesha@example.com", password="correct horse battery staple", dob="1990-05-14"),
    )

    with pytest.raises(AppError) as wrong_password:
        login(db_session, LoginRequest(email="ayesha@example.com", password="wrong"))

    with pytest.raises(AppError) as no_such_email:
        login(db_session, LoginRequest(email="nobody@example.com", password="whatever123"))

    # Both must be genuinely indistinguishable -- same status, same code,
    # same message. Anything different here is an information leak.
    assert wrong_password.value.status_code == no_such_email.value.status_code == 401
    assert wrong_password.value.code == no_such_email.value.code == "INVALID_CREDENTIALS"
    assert wrong_password.value.message == no_such_email.value.message


def test_login_rejects_a_deactivated_account(db_session: Session) -> None:
    user = register_patient(
        db_session,
        RegisterRequest(email="ayesha@example.com", password="correct horse battery staple", dob="1990-05-14"),
    )
    user.is_active = False
    db_session.flush()

    with pytest.raises(AppError) as exc_info:
        login(
            db_session,
            LoginRequest(email="ayesha@example.com", password="correct horse battery staple"),
        )
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "INVALID_CREDENTIALS"

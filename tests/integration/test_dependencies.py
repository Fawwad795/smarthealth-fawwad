"""get_current_user against a real database -- an authenticated User row,
or a 401, for every failure mode.
"""

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.exceptions import AppError
from app.core.security import create_access_token
from app.models import User
from app.models.enums import UserRole


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _make_user(db_session: Session, email: str = "user@example.com") -> User:
    user = User(email=email, password_hash="not-a-real-hash", role=UserRole.PATIENT)
    db_session.add(user)
    db_session.flush()
    return user


def test_valid_token_returns_the_matching_user(db_session: Session) -> None:
    user = _make_user(db_session)
    token = create_access_token(user.id)

    result = get_current_user(credentials=_credentials(token), db=db_session)

    assert result.id == user.id


def test_missing_credentials_raise_401(db_session: Session) -> None:
    with pytest.raises(AppError) as exc_info:
        get_current_user(credentials=None, db=db_session)
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "NOT_AUTHENTICATED"


def test_token_for_a_deactivated_user_is_rejected(db_session: Session) -> None:
    user = _make_user(db_session)
    token = create_access_token(user.id)
    user.is_active = False
    db_session.flush()

    with pytest.raises(AppError) as exc_info:
        get_current_user(credentials=_credentials(token), db=db_session)
    assert exc_info.value.status_code == 401


def test_token_for_a_nonexistent_user_id_is_rejected(db_session: Session) -> None:
    token = create_access_token(999999999)
    with pytest.raises(AppError) as exc_info:
        get_current_user(credentials=_credentials(token), db=db_session)
    assert exc_info.value.status_code == 401

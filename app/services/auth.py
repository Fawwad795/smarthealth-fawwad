"""Business rules for registration and login. Routers stay thin -- this is
where "is this email taken", "does this password match" and "what does a
login actually return" live.
"""

from fastapi import status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.security import create_access_token, hash_password, verify_password
from app.models import Patient, User
from app.models.enums import UserRole
from app.schemas.auth import LoginRequest, RegisterRequest


def register_patient(db: Session, data: RegisterRequest) -> User:
    """Create a User + Patient pair, or fail with 409 if the email is
    already taken. One transaction: a User with no Patient row, or a
    Patient row with no User, should never be possible to create.
    """
    email = data.email.lower()

    existing = db.query(User).filter(func.lower(User.email) == email).first()
    if existing is not None:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="EMAIL_TAKEN",
            message="An account with this email already exists.",
        )

    user = User(
        email=email, password_hash=hash_password(data.password), role=UserRole.PATIENT
    )
    db.add(user)
    db.flush()  # assigns user.id, still inside this transaction

    patient = Patient(user_id=user.id, dob=data.dob)
    db.add(patient)

    db.commit()
    db.refresh(user)
    return user


def login(db: Session, data: LoginRequest) -> str:
    """Verify credentials and return a signed access token, or fail with a
    single generic 401.

    Deliberately the same error whether the email doesn't exist, the
    password is wrong, or the account is deactivated -- distinguishing any
    of these would tell an attacker which emails have real accounts here.
    """
    email = data.email.lower()
    user = db.query(User).filter(func.lower(User.email) == email).first()

    invalid = AppError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        code="INVALID_CREDENTIALS",
        message="Incorrect email or password.",
    )

    if user is None or not user.is_active:
        raise invalid
    if not verify_password(data.password, user.password_hash):
        raise invalid

    return create_access_token(user.id)

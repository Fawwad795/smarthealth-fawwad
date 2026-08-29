"""Auth routes: register, login. Thin -- everything real lives in
services/auth.py.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserMeResponse,
)
from app.services import auth as auth_service
from app.core.dependencies import get_current_user
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED
)
def register(data: RegisterRequest, db: Session = Depends(get_db)) -> RegisterResponse:
    """Create a patient account. Public: no authentication required.

    Patient-only by design -- there is no role field on the request, so
    this route cannot mint a provider, front_desk or admin account.
    """
    user = auth_service.register_patient(db, data)
    return RegisterResponse(id=user.id, email=user.email)


@router.post("/login", response_model=TokenResponse)
def login(data: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Exchange credentials for an access token. Public.

    Returns one identical 401 whether the email is unknown, the password
    is wrong, or the account is deactivated -- see services/auth.py.
    """
    token = auth_service.login(db, data)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserMeResponse)
def me(current_user: User = Depends(get_current_user)) -> UserMeResponse:
    """Return the authenticated caller's own identity.

    The simplest protected endpoint in the system, and the one used to
    confirm a token is valid. Any authenticated role may call it; the
    response is built field-by-field so no password_hash can escape.
    """
    return UserMeResponse(
        id=current_user.id, email=current_user.email, role=current_user.role
    )

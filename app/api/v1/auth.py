"""Auth routes: register, login. Thin -- everything real lives in
services/auth.py.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserMeResponse
)
from app.services import auth as auth_service
from app.core.dependencies import get_current_user
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(data: RegisterRequest, db: Session = Depends(get_db)) -> RegisterResponse:
    user = auth_service.register_patient(db, data)
    return RegisterResponse(id=user.id, email=user.email)


@router.post("/login", response_model=TokenResponse)
def login(data: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    token = auth_service.login(db, data)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserMeResponse)
def me(current_user: User = Depends(get_current_user)) -> UserMeResponse:
    return UserMeResponse(id=current_user.id, email=current_user.email, role=current_user.role)

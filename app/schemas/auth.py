"""Request/response shapes for the auth endpoints. Never return an ORM
object from a router -- these are what stand between password_hash and a
client.
"""

from datetime import date

from pydantic import BaseModel, EmailStr, Field

from app.models.enums import UserRole


class RegisterRequest(BaseModel):
    """Public registration is patient-only. Provider, front_desk and admin
    accounts are provisioned by the seed script (task 1.10), not created
    through a route anyone on the internet can hit.
    """

    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    dob: date


class RegisterResponse(BaseModel):
    id: int
    email: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserMeResponse(BaseModel):
    id: int
    email: str
    role: UserRole
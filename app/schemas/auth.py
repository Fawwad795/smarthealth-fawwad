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
    """What a successful registration returns: enough to confirm which
    account was created, and nothing else. No token -- registering and
    logging in are separate steps -- and above all no password_hash.
    """

    id: int
    email: str


class LoginRequest(BaseModel):
    """Credentials for POST /auth/login.

    No length constraints on password, unlike RegisterRequest: this field
    is only ever compared against a stored hash, never used to create one,
    and rejecting an over-length password here would tell an attacker
    something about what is stored.
    """

    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """A successful login: the signed JWT plus its type.

    token_type is "bearer" to tell the client how to send it back --
    `Authorization: Bearer <token>`, which is what HTTPBearer expects.
    """

    access_token: str
    token_type: str = "bearer"


class UserMeResponse(BaseModel):
    """The authenticated caller's own identity, for GET /auth/me.

    Deliberately only three fields: this is built from a User row, which
    also holds password_hash and is_active, and naming fields explicitly
    is what keeps those from ever being serialised.
    """

    id: int
    email: str
    role: UserRole

"""Shared FastAPI dependencies -- the things routers Depends() on.

get_current_user is the one every protected endpoint in this project will
eventually depend on: it turns a request's Authorization header into an
authenticated User row, or fails with 401.
"""

from fastapi import Depends, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import User, Patient
from app.models.enums import UserRole

# auto_error=False: FastAPI's own default response to a *missing* header is
# a 403, not 401 -- but the brief requires missing, invalid and expired
# tokens to all fail the same way. Turning auto_error off means this
# function raises the 401 itself, for every failure mode, uniformly.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Turn a request's Authorization header into an authenticated User row.

    Raises 401 for every failure mode -- missing header, invalid or expired
    token, unknown user id, deactivated account -- so none of them can be
    told apart by a caller probing the endpoint.

    is_active is re-checked here even though login() already checked it: a
    token stays cryptographically valid for its whole lifetime, so an
    account deactivated after issue is only caught by a per-request check.
    """
    if credentials is None:
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="NOT_AUTHENTICATED",
            message="Not authenticated.",
        )

    user_id = decode_access_token(credentials.credentials)

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="NOT_AUTHENTICATED",
            message="Not authenticated.",
        )

    return user


def require_role(*allowed_roles: UserRole):
    """Dependency factory: require_role(UserRole.PROVIDER) builds a
    dependency that lets a provider through and 403s everyone else.

    A factory rather than a single function because "which roles are
    allowed" differs per endpoint -- a provider-schedule route and a
    reports route need different answers, and each needs its own
    dependency instance built from the roles that specific route allows.
    """

    def _require_role(current_user: User = Depends(get_current_user)) -> User:
        """The dependency FastAPI actually resolves: authenticate first
        (via get_current_user), then check the role against the closure's
        allowed_roles, 403 if it isn't one of them."""
        if current_user.role not in allowed_roles:
            raise AppError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="FORBIDDEN",
                message="You do not have permission to perform this action.",
            )
        return current_user

    return _require_role


def ensure_patient_self_or_staff(current_user: User, patient: Patient) -> None:
    """Is current_user allowed to see this specific patient's data?

    Not a role check: PATIENT is a role every patient account holds, but
    that says nothing about *which* patient's data someone may see. Not a
    Depends() dependency either -- it needs the actual Patient row, which
    only exists once a router has already loaded one, typically from a
    path parameter such as /patients/{id}.
    """
    if current_user.role in (UserRole.FRONT_DESK, UserRole.ADMIN):
        return
    if current_user.role == UserRole.PATIENT and current_user.id == patient.user_id:
        return
    raise AppError(
        status_code=status.HTTP_403_FORBIDDEN,
        code="FORBIDDEN",
        message="You do not have permission to access this patient's data.",
    )

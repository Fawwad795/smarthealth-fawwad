"""Shared FastAPI dependencies -- the things routers Depends() on.

get_current_user is the one every protected endpoint in this project will
eventually depend on: it turns a request's Authorization header into an
authenticated User row, or fails with 401.
"""

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import User

# auto_error=False: FastAPI's own default response to a *missing* header is
# a 403, not 401 -- but the brief requires missing, invalid and expired
# tokens to all fail the same way. Turning auto_error off means this
# function raises the 401 itself, for every failure mode, uniformly.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise AppError(
            status_code=401, code="NOT_AUTHENTICATED", message="Not authenticated."
        )

    user_id = decode_access_token(credentials.credentials)

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise AppError(
            status_code=401, code="NOT_AUTHENTICATED", message="Not authenticated."
        )

    return user
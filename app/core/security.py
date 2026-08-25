"""Password hashing. The only place bcrypt is called from -- nothing else
in the codebase should import passlib directly.
"""

from passlib.context import CryptContext

from datetime import datetime, timedelta, timezone

from fastapi import status
from jose import JWTError, jwt

from app.core.config import settings
from app.core.exceptions import AppError


# deprecated="auto" future-proofs this: adding a stronger scheme to the
# front of the list later still verifies existing hashes fine, and
# re-hashes them with the new scheme on next successful login -- no
# migration needed.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Turn a plaintext password into the string stored in password_hash.

    bcrypt generates a random salt per call, so hashing the same password
    twice produces two different strings -- both verify correctly.
    """
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Does this plaintext password match this stored hash?

    Never reverses the hash -- that's not possible. It re-hashes `plain`
    using the algorithm and salt recorded inside `hashed`, and compares
    the two hashes.
    """
    return _pwd_context.verify(plain, hashed)


def create_access_token(user_id: int) -> str:
    """Issue a signed token proving "this is user_id", valid for
    settings.access_token_expire_minutes.

    "sub" (subject) is the JWT spec's name for who the token is about, and
    it must be a string -- python-jose won't accept a bare int here.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {"sub": str(user_id), "exp": expires_at}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> int:
    """Verify a token's signature and expiry, and return the user id inside
    it. Raises AppError -- never a raw jose exception -- so a missing,
    tampered, expired or malformed token all become the same clean 401,
    whatever called this doesn't need to know which.
    """
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_TOKEN",
            message="Invalid or expired token",
        )
"""Password hashing and JWT issue/verify. No database, no infrastructure --
these are pure functions wrapping passlib and python-jose.
"""

from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt as jose_jwt

from app.core.config import settings
from app.core.exceptions import AppError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)



def test_hashing_the_same_password_twice_yields_different_hashes() -> None:
    """bcrypt salts every call independently. If two users pick the same
    password, their stored hashes must not reveal that -- and if this test
    ever started failing, it would mean the salt generation broke.
    """
    password = "correct horse battery staple"
    first = hash_password(password)
    second = hash_password(password)
    assert first != second
    assert verify_password(password, first)
    assert verify_password(password, second)


def test_wrong_password_does_not_verify() -> None:
    hashed = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", hashed)


def test_hash_has_the_expected_bcrypt_shape() -> None:
    """$2b$ <cost> $ <22-char salt><31-char digest> -- 60 characters total.
    A stray change of scheme (e.g. an accidental argon2 fallback) would
    change this shape, and password_hash is sized (255) with bcrypt's 60
    characters in mind.
    """
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$2b$")
    assert len(hashed) == 60


def test_bcrypt_only_considers_the_first_72_bytes() -> None:
    """A documented bcrypt limitation, not a bug in this code: anything
    past 72 bytes is silently ignored, so two different passwords sharing
    the same first 72 bytes verify against the same hash. Worth knowing
    before it surprises anyone reading a login failure in the wild -- a
    password manager generating very long random passwords is exactly the
    case where this would bite.
    """
    hashed = hash_password("x" * 100)
    assert verify_password("x" * 72 + "one tail", hashed)
    assert verify_password("x" * 72 + "a different tail", hashed)


def test_token_round_trip_returns_the_same_user_id() -> None:
    token = create_access_token(42)
    assert decode_access_token(token) == 42


def test_tampered_token_is_rejected() -> None:
    """Changing even one character invalidates the signature -- this is
    the entire security model. Without this check, anyone could edit the
    payload to claim to be a different user id.

    Flips a character in the middle of the signature segment, not the
    last character of the whole token: a 256-bit HMAC-SHA256 signature's
    final base64url character only encodes 4 real bits, with the other 2
    discarded as padding on decode -- so about 1 in 4 possible
    last-character replacements silently decode to the exact same
    signature bytes, leaving a token that is not actually tampered. A
    middle character has no such padding, so every replacement genuinely
    changes the decoded bytes.
    """
    token = create_access_token(42)
    header, payload, signature = token.split(".")
    middle = len(signature) // 2
    flipped_char = "a" if signature[middle] != "a" else "b"
    tampered_signature = signature[:middle] + flipped_char + signature[middle + 1 :]
    tampered = f"{header}.{payload}.{tampered_signature}"

    with pytest.raises(AppError) as exc_info:
        decode_access_token(tampered)
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "INVALID_TOKEN"


def test_expired_token_is_rejected() -> None:
    """Crafted directly with jose rather than by sleeping past
    access_token_expire_minutes -- a signature can be perfectly genuine
    and still be too old to trust.
    """
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    token = jose_jwt.encode(
        {"sub": "42", "exp": expired_at},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(AppError) as exc_info:
        decode_access_token(token)
    assert exc_info.value.status_code == 401


def test_garbage_token_is_rejected() -> None:
    with pytest.raises(AppError):
        decode_access_token("not.a.token")

"""Password hashing. No database, no infrastructure -- these are pure
functions wrapping passlib.
"""

from app.core.security import hash_password, verify_password


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

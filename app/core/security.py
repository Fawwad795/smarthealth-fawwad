"""Password hashing. The only place bcrypt is called from -- nothing else
in the codebase should import passlib directly.
"""

from passlib.context import CryptContext

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
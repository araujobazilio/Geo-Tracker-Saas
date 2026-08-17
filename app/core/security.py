"""Security primitives.

Phase 0 only exposes password hashing (Argon2id) and a constant-time
comparison helper. Session/CSRF/token handling will be added in Phase 2.
"""

from __future__ import annotations

from secrets import compare_digest

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(plain: str) -> str:
    """Hash a plaintext password using Argon2id."""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if `plain` matches `hashed` (constant-time on success)."""
    try:
        return _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception:
        # Malformed hash etc. — never leak details; treat as no match.
        return False


def safe_eq(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    return compare_digest(a.encode("utf-8"), b.encode("utf-8"))

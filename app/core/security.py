"""Security primitives.

Password hashing (Argon2id), constant-time comparison, session token
generation, CSRF token generation, email normalization, and password
policy validation.
"""

from __future__ import annotations

import hashlib
import re
from secrets import compare_digest, token_urlsafe

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

# --- Password policy ---
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

# Simple email format regex (practical validation, not RFC-exhaustive).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def check_needs_rehash(hashed: str) -> bool:
    """Return True if the stored hash parameters differ from current defaults."""
    return _hasher.check_needs_rehash(hashed)


def safe_eq(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    return compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --- Session tokens ---


def generate_session_token() -> str:
    """Generate a cryptographically secure opaque session token (43 chars, 256 bits)."""
    return token_urlsafe(32)


def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token (43 chars, 256 bits)."""
    return token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hash a session/CSRF token for safe storage as a Redis key.

    The browser receives the raw token; the server stores only the hash.
    This reduces exposure if Redis keys are inspected.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- Email normalization ---


def normalize_email(email: str) -> str:
    """Normalize an email address to a canonical lowercase form.

    - strips surrounding whitespace
    - lowercases the entire address (domain + local part)

    This is the canonical strategy for GEO Tracker: store and compare
    emails in lowercase. This prevents practical duplicates caused by
    casing differences (e.g. Alice@Example.com vs alice@example.com).
    """
    return email.strip().lower()


def validate_email_format(email: str) -> bool:
    """Return True if the email has a valid practical format."""
    if not email or len(email) > 255:
        return False
    return bool(_EMAIL_RE.match(email))


# --- Password policy ---


def validate_password(password: str) -> bool:
    """Return True if the password meets the minimum policy.

    Policy:
    - minimum 12 characters
    - maximum 128 characters
    - no arbitrary character-class requirements
    """
    return MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH

"""Unit tests for security primitives."""

from __future__ import annotations

from app.core.security import hash_password, safe_eq, verify_password


def test_hash_and_verify_password_roundtrip() -> None:
    plain = "correct horse battery staple"
    hashed = hash_password(plain)
    assert hashed != plain
    assert verify_password(plain, hashed) is True


def test_verify_password_rejects_wrong_password() -> None:
    hashed = hash_password("the right one")
    assert verify_password("the wrong one", hashed) is False


def test_verify_password_rejects_malformed_hash() -> None:
    assert verify_password("anything", "not-a-valid-hash") is False


def test_safe_eq_matches() -> None:
    assert safe_eq("abc", "abc") is True


def test_safe_eq_differs() -> None:
    assert safe_eq("abc", "abd") is False

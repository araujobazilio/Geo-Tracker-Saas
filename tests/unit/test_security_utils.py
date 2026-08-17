"""Unit tests for security utilities (email normalization, password policy, tokens)."""

from __future__ import annotations

from app.core.security import (
    check_needs_rehash,
    generate_csrf_token,
    generate_session_token,
    hash_password,
    hash_token,
    normalize_email,
    safe_eq,
    validate_email_format,
    validate_password,
    verify_password,
)


class TestEmailNormalization:
    def test_lowercases_email(self) -> None:
        assert normalize_email("Alice@Example.COM") == "alice@example.com"

    def test_strips_whitespace(self) -> None:
        assert normalize_email("  alice@example.com  ") == "alice@example.com"

    def test_already_normalized(self) -> None:
        assert normalize_email("alice@example.com") == "alice@example.com"


class TestEmailValidation:
    def test_valid_email(self) -> None:
        assert validate_email_format("alice@example.com") is True

    def test_invalid_no_at(self) -> None:
        assert validate_email_format("notanemail") is False

    def test_invalid_no_domain(self) -> None:
        assert validate_email_format("alice@") is False

    def test_invalid_empty(self) -> None:
        assert validate_email_format("") is False

    def test_invalid_too_long(self) -> None:
        assert validate_email_format("a" * 256 + "@example.com") is False


class TestPasswordPolicy:
    def test_valid_password(self) -> None:
        assert validate_password("secure-password-123") is True

    def test_too_short(self) -> None:
        assert validate_password("short123") is False

    def test_exactly_min(self) -> None:
        assert validate_password("a" * 12) is True

    def test_exactly_max(self) -> None:
        assert validate_password("a" * 128) is True

    def test_too_long(self) -> None:
        assert validate_password("a" * 129) is False

    def test_empty(self) -> None:
        assert validate_password("") is False


class TestPasswordHashing:
    def test_hash_and_verify(self) -> None:
        h = hash_password("my-password-123")
        assert verify_password("my-password-123", h) is True

    def test_wrong_password(self) -> None:
        h = hash_password("correct-password-12")
        assert verify_password("wrong-password-12", h) is False

    def test_check_needs_rehash_false(self) -> None:
        h = hash_password("test-password-123")
        assert check_needs_rehash(h) is False


class TestSessionTokens:
    def test_session_token_unique(self) -> None:
        assert generate_session_token() != generate_session_token()

    def test_csrf_token_unique(self) -> None:
        assert generate_csrf_token() != generate_csrf_token()

    def test_hash_token_is_hex(self) -> None:
        h = hash_token("test-token")
        assert len(h) == 64
        int(h, 16)  # valid hex

    def test_hash_token_deterministic(self) -> None:
        assert hash_token("abc") == hash_token("abc")

    def test_hash_token_different_inputs(self) -> None:
        assert hash_token("abc") != hash_token("xyz")


class TestSafeEq:
    def test_equal(self) -> None:
        assert safe_eq("abc", "abc") is True

    def test_not_equal(self) -> None:
        assert safe_eq("abc", "xyz") is False

    def test_different_length(self) -> None:
        assert safe_eq("abc", "abcd") is False

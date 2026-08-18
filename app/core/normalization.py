"""Centralized normalization utilities for domains, keywords, countries, languages, and brand aliases.

These utilities perform LOCAL normalization only — no network requests,
no DNS lookups, no website fetches. They are deterministic and safe to
call in any context.
"""

from __future__ import annotations

import re
import unicodedata

from app.core.exceptions import ValidationError

# Maximum lengths for normalization targets.
_MAX_HOSTNAME_LENGTH = 253
_MAX_KEYWORD_LENGTH = 500
_MAX_BRAND_ALIAS_LENGTH = 255
_MAX_BRAND_ALIASES = 50

# Supported language families for deterministic prompt generation.
_LANGUAGE_FAMILIES: dict[str, str] = {
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "pt": "pt",
    "pt-br": "pt",
    "pt-pt": "pt",
}

_SUPPORTED_LANGUAGE_CODES = set(_LANGUAGE_FAMILIES.keys())


def normalize_domain(domain: str) -> str:
    """Normalize a domain to a canonical hostname.

    - lowercase
    - strip scheme (http://, https://)
    - strip path / query / fragment
    - strip trailing dot
    - strip leading www.
    - validate hostname format

    Returns the canonical hostname (e.g. "example.com").

    Raises ValidationError if the domain is empty or invalid.
    """
    if not domain or not domain.strip():
        raise ValidationError("Domain must not be empty.")

    raw = domain.strip()

    # Strip scheme.
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)

    # Strip path / query / fragment.
    raw = raw.split("/")[0].split("?")[0].split("#")[0]

    # Strip userinfo (user:pass@).
    if "@" in raw:
        raw = raw.split("@")[-1]

    # Strip port.
    raw = raw.split(":")[0]

    # Lowercase.
    raw = raw.lower().strip()

    # Strip trailing dot.
    raw = raw.rstrip(".")

    # Strip leading www.
    raw = re.sub(r"^www\.", "", raw)

    if not raw:
        raise ValidationError("Domain must not be empty after normalization.")

    if len(raw) > _MAX_HOSTNAME_LENGTH:
        raise ValidationError(f"Domain too long (max {_MAX_HOSTNAME_LENGTH} characters).")

    # Basic hostname validation: letters, digits, hyphens, dots.
    # Each label must not start/end with hyphen.
    if not re.match(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", raw):
        raise ValidationError(f"Invalid hostname: {raw}")

    # Validate each label.
    for label in raw.split("."):
        if not label:
            raise ValidationError(f"Invalid hostname (empty label): {raw}")
        if len(label) > 63:
            raise ValidationError(f"Invalid hostname (label too long): {raw}")
        if label.startswith("-") or label.endswith("-"):
            raise ValidationError(f"Invalid hostname (hyphen in label): {raw}")

    return raw


def normalize_keyword(text: str) -> tuple[str, str]:
    """Normalize a keyword for storage and uniqueness.

    Returns a tuple of (display_text, normalized_text):
    - display_text: trimmed outer whitespace, collapsed internal whitespace
    - normalized_text: lowercased version of display_text for uniqueness

    Raises ValidationError if the keyword is empty or too long.
    """
    if not text or not text.strip():
        raise ValidationError("Keyword must not be empty.")

    # Trim outer whitespace and collapse internal whitespace.
    display = re.sub(r"\s+", " ", text.strip())

    if not display:
        raise ValidationError("Keyword must not be empty after normalization.")

    if len(display) > _MAX_KEYWORD_LENGTH:
        raise ValidationError(f"Keyword too long (max {_MAX_KEYWORD_LENGTH} characters).")

    normalized = display.lower()
    return display, normalized


def normalize_country(country: str | None) -> str | None:
    """Normalize a country code to uppercase ISO-style.

    Returns None if input is None or empty.
    Raises ValidationError if the code is not a 2-letter alpha code.
    """
    if country is None:
        return None
    result = country.strip().upper()
    if not result:
        return None
    if not re.match(r"^[A-Z]{2}$", result):
        raise ValidationError(
            f"Invalid country code: {country}. Use 2-letter ISO code (e.g. US, BR)."
        )
    return result


def normalize_language(language: str | None) -> str | None:
    """Normalize a language code.

    Accepts formats like: en, en-US, pt, pt-BR.
    Returns the normalized form (e.g. "en", "pt-BR").

    Returns None if input is None or empty.
    Raises ValidationError if the language is not supported for
    deterministic prompt generation.
    """
    if language is None:
        return None
    result = language.strip().lower()
    if not result:
        return None

    # Normalize to xx or xx-YY form.
    parts = result.split("-")
    if len(parts) == 1:
        normalized = parts[0]
    elif len(parts) >= 2:
        normalized = f"{parts[0]}-{parts[1]}"
    else:
        raise ValidationError(f"Invalid language code: {language}.")

    if not re.match(r"^[a-z]{2}(-[a-z]{2})?$", normalized):
        raise ValidationError(f"Invalid language code: {language}.")

    if normalized not in _SUPPORTED_LANGUAGE_CODES:
        raise ValidationError(
            f"Language '{normalized}' is not supported for deterministic prompt generation. "
            f"Supported: {', '.join(sorted(_SUPPORTED_LANGUAGE_CODES))}."
        )

    return normalized


def get_language_family(language: str | None) -> str:
    """Return the language family (e.g. 'en', 'pt') for a normalized language code.

    Raises ValidationError if the language is not supported.
    """
    if language is None:
        # Default to English if no language specified.
        return "en"
    family = _LANGUAGE_FAMILIES.get(language)
    if family is None:
        raise ValidationError(
            f"Language '{language}' is not supported for deterministic prompt generation."
        )
    return family


def normalize_brand_aliases(aliases: list[str] | None) -> list[str]:
    """Normalize brand aliases.

    - trim whitespace
    - remove empty strings
    - deduplicate case-insensitively
    - preserve first-seen display form
    - enforce maximum count and length

    Returns the normalized list.
    """
    if not aliases:
        return []

    seen: set[str] = set()
    result: list[str] = []

    for alias in aliases:
        if alias is None:
            continue
        trimmed = alias.strip()
        if not trimmed:
            continue
        if len(trimmed) > _MAX_BRAND_ALIAS_LENGTH:
            raise ValidationError(
                f"Brand alias too long (max {_MAX_BRAND_ALIAS_LENGTH} characters): {trimmed}"
            )
        lower = trimmed.lower()
        if lower not in seen:
            seen.add(lower)
            result.append(trimmed)

    if len(result) > _MAX_BRAND_ALIASES:
        raise ValidationError(f"Too many brand aliases (max {_MAX_BRAND_ALIASES}).")

    return result


def normalize_text_for_comparison(text: str) -> str:
    """Normalize text for case-insensitive, accent-insensitive comparison.

    Used for checking if brand names/aliases appear in NON_BRANDED prompts.
    """
    # NFKD decomposition to separate base chars from diacritics.
    normalized = unicodedata.normalize("NFKD", text)
    # Remove combining characters (diacritics).
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return normalized.lower().strip()

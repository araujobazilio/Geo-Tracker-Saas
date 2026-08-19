"""Deterministic entity mention detection in response text.

This module implements case-insensitive, boundary-aware, longest-match
detection of entity names, aliases, and domains in PromptRun response
text. It is fully deterministic — no LLM, no embeddings, no external
calls.

Key rules:
- Case-insensitive Unicode-aware matching.
- Token/phrase boundary semantics (no substring false positives).
- Multi-word terms match natural whitespace variants.
- Overlapping terms for the same entity deduplicate to the longest match.
- Ambiguous terms shared by multiple entities are excluded from
  automatic attribution and produce a warning.
- Domains are matched as standalone tokens (e.g. ``acme.com`` in text).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.enums import EntityMatchType


class WarningCode(str, Enum):
    AMBIGUOUS_ENTITY_TERM = "AMBIGUOUS_ENTITY_TERM"
    INVALID_SOURCE_URL = "INVALID_SOURCE_URL"
    AMBIGUOUS_SOURCE_DOMAIN = "AMBIGUOUS_SOURCE_DOMAIN"


@dataclass(frozen=True)
class EntityTerm:
    """A normalized term belonging to a specific entity snapshot."""

    entity_snapshot_id: str
    term: str  # original display form
    normalized: str  # lowercased, NFKC normalized
    match_type: EntityMatchType


@dataclass(frozen=True)
class MentionMatch:
    """One detected occurrence of an entity term in response text."""

    entity_snapshot_id: str
    match_type: EntityMatchType
    matched_text: str  # original substring from the response
    matched_term: str  # the configured term that matched
    start_index: int
    end_index: int


@dataclass
class DetectionResult:
    """Result of mention detection for one response text."""

    mentions: list[MentionMatch] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)  # (code, detail)


def _normalize_text(text: str) -> str:
    """NFKC normalize and collapse internal whitespace for matching."""
    return unicodedata.normalize("NFKC", text)


def _normalize_term(term: str) -> str:
    """Normalize a configured entity term for comparison."""
    normalized = unicodedata.normalize("NFKC", term.strip())
    # Collapse internal whitespace to single spaces for multi-word terms.
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.lower()


def _normalize_domain(domain: str) -> str:
    """Normalize a domain: lowercase, strip trailing dot, strip www. prefix."""
    d = domain.strip().lower().rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return d


# Characters that count as word boundaries for matching purposes.
# This includes whitespace, punctuation, and string start/end.
# Unicode-aware: uses \b which respects Unicode word characters in Python's
# re module with re.UNICODE (default for str patterns).
_WORD_BOUNDARY = r"(?<![^\W_])"  # negative lookbehind: not preceded by word char
_WORD_BOUNDARY_END = r"(?![^\W_])"  # negative lookahead: not followed by word char


def _build_term_pattern(normalized_term: str) -> re.Pattern[str]:
    """Build a regex pattern for a normalized term with boundary semantics.

    For multi-word terms, allows flexible whitespace between words
    (one or more whitespace characters).
    """
    # Escape regex special characters, then allow flexible whitespace
    # between words.
    words = re.escape(normalized_term).split(r"\ ")
    if len(words) == 1:
        pattern = _WORD_BOUNDARY + words[0] + _WORD_BOUNDARY_END
    else:
        pattern = _WORD_BOUNDARY + r"\s+".join(words) + _WORD_BOUNDARY_END
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


def _build_domain_pattern(normalized_domain: str) -> re.Pattern[str]:
    """Build a regex pattern for domain mention in text.

    Domains appear as tokens like ``acme.com`` or ``www.acme.com``.
    We match the domain optionally preceded by ``www.`` and ensure
    it's bounded by non-domain characters (whitespace, punctuation,
    start/end).
    """
    escaped = re.escape(normalized_domain)
    # Allow optional www. prefix and optional scheme/path prefix.
    pattern = r"(?:(?:https?://)?(?:www\.)?)" + escaped + _WORD_BOUNDARY_END
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


def build_entity_terms(
    snapshots: list[dict[str, Any]],
) -> tuple[list[EntityTerm], list[tuple[str, str]]]:
    """Build deduplicated entity terms from snapshot dicts.

    Each snapshot dict must have: entity_snapshot_id (str), name (str),
    domain (str), aliases (list[str]).

    Returns (terms, warnings) where warnings is a list of
    (code, detail) tuples for ambiguous terms.

    Term ownership map: if the same normalized NAME/ALIAS term belongs
    to multiple entities, that term is excluded from all entities and
    a warning is recorded. Domains remain separately attributable.
    """
    warnings: list[tuple[str, str]] = []

    # Map normalized term -> set of entity_snapshot_ids that own it.
    term_owners: dict[str, set[str]] = {}
    # Map (entity_snapshot_id, normalized_term) -> EntityTerm
    # Map (entity_snapshot_id, normalized_term) -> EntityTerm
    entity_terms_map: dict[tuple[str, str], EntityTerm] = {}

    for snap in snapshots:
        eid = snap["entity_snapshot_id"]
        # Name
        name_norm = _normalize_term(snap["name"])
        if name_norm:
            key = (eid, name_norm)
            if key not in entity_terms_map:
                entity_terms_map[key] = EntityTerm(
                    entity_snapshot_id=eid,
                    term=snap["name"],
                    normalized=name_norm,
                    match_type=EntityMatchType.NAME,
                )
                term_owners.setdefault(name_norm, set()).add(eid)

        # Aliases
        for alias in snap.get("aliases", []):
            alias_norm = _normalize_term(alias)
            if not alias_norm:
                continue
            key = (eid, alias_norm)
            if key not in entity_terms_map:
                entity_terms_map[key] = EntityTerm(
                    entity_snapshot_id=eid,
                    term=alias,
                    normalized=alias_norm,
                    match_type=EntityMatchType.ALIAS,
                )
                term_owners.setdefault(alias_norm, set()).add(eid)

        # Domain
        domain_norm = _normalize_domain(snap["domain"])
        if domain_norm:
            key = (eid, domain_norm)
            if key not in entity_terms_map:
                entity_terms_map[key] = EntityTerm(
                    entity_snapshot_id=eid,
                    term=snap["domain"],
                    normalized=domain_norm,
                    match_type=EntityMatchType.DOMAIN,
                )
                # Domains are NOT subject to ambiguity exclusion —
                # they are separately attributable. But we still track
                # for informational purposes. Actually, per spec, domains
                # remain separately attributable even if shared. So we
                # don't add domains to the term_owners ambiguity check.

    # Identify ambiguous terms (owned by >1 entity, NAME/ALIAS only).
    ambiguous_terms: set[str] = set()
    for term, owners in term_owners.items():
        if len(owners) > 1:
            ambiguous_terms.add(term)
            warnings.append(
                (
                    WarningCode.AMBIGUOUS_ENTITY_TERM.value,
                    f"Term '{term}' is shared by {len(owners)} entities; excluded from automatic attribution.",
                )
            )

    # Build final term list, excluding ambiguous NAME/ALIAS terms.
    terms: list[EntityTerm] = []
    for (_eid, norm), entity_term in entity_terms_map.items():
        if (
            entity_term.match_type in (EntityMatchType.NAME, EntityMatchType.ALIAS)
            and norm in ambiguous_terms
        ):
            continue
        terms.append(entity_term)

    return terms, warnings


def detect_mentions(
    response_text: str,
    entity_terms: list[EntityTerm],
) -> DetectionResult:
    """Detect all entity mentions in a response text.

    Returns mentions ordered by their position in the response text.
    Overlapping matches for the same entity are deduplicated to the
    longest match. Genuinely distinct occurrences are preserved.

    The occurrence_index assigned to each mention follows the original
    response order (1-based).
    """
    if not response_text:
        return DetectionResult()

    # Build patterns for each term.
    patterns: list[tuple[re.Pattern[str], EntityTerm]] = []
    for term in entity_terms:
        if term.match_type == EntityMatchType.DOMAIN:
            pattern = _build_domain_pattern(term.normalized)
        else:
            pattern = _build_term_pattern(term.normalized)
        patterns.append((pattern, term))

    # Find all raw matches: (start, end, matched_text, EntityTerm)
    raw_matches: list[tuple[int, int, str, EntityTerm]] = []
    for pattern, term in patterns:
        for match in pattern.finditer(response_text):
            raw_matches.append(
                (
                    match.start(),
                    match.end(),
                    match.group(),
                    term,
                )
            )

    if not raw_matches:
        return DetectionResult()

    # Sort by start position, then by length descending (longest first).
    raw_matches.sort(key=lambda m: (m[0], -(m[1] - m[0])))

    # Deduplicate overlapping matches for the same entity.
    # Strategy: iterate in position order. For each match, check if it
    # overlaps with an already-accepted match for the SAME entity.
    # If it does and is shorter or equal, skip it (longest wins).
    # If it overlaps with a different entity's match, keep both
    # (different entities can overlap).
    accepted: list[tuple[int, int, str, EntityTerm]] = []
    for start, end, text, term in raw_matches:
        overlaps_same_entity = False
        for a_start, a_end, _, a_term in accepted:
            if (
                a_term.entity_snapshot_id == term.entity_snapshot_id
                and start < a_end
                and end > a_start
            ):
                overlaps_same_entity = True
                break
        if not overlaps_same_entity:
            accepted.append((start, end, text, term))

    # Sort accepted by start position for final ordering.
    accepted.sort(key=lambda m: m[0])

    mentions = [
        MentionMatch(
            entity_snapshot_id=term.entity_snapshot_id,
            match_type=term.match_type,
            matched_text=text,
            matched_term=term.term,
            start_index=start,
            end_index=end,
        )
        for start, end, text, term in accepted
    ]

    return DetectionResult(mentions=mentions)

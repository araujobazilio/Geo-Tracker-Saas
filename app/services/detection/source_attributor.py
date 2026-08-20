"""Deterministic source attribution via domain matching.

Attributes ResponseSource URLs to tracked entities by comparing the
source hostname against entity domains. Uses safe urllib parsing only —
no DNS, no HTTP, no redirect resolution.

Key rules:
- Exact domain match or subdomain match (host == domain OR
  host ends with "." + domain).
- Most-specific domain wins when multiple tracked domains match.
- Ambiguous matches (multiple equally-specific domains) are NOT
  attributed and produce an AMBIGUOUS_SOURCE_DOMAIN warning.
- Invalid URLs are skipped with a warning, not a failure.
- Source title/entity semantic analysis is NOT performed — only
  hostname attribution counts as "owned citation".
"""

from __future__ import annotations

import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.services.detection.mention_detector import WarningCode


class AttributionOutcome(str, Enum):
    """Outcome of attempting to attribute a single source."""

    NO_MATCH = "NO_MATCH"
    ATTRIBUTED = "ATTRIBUTED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class EntityDomain:
    """A tracked entity domain for attribution."""

    entity_snapshot_id: str
    domain: str  # normalized: lowercase, no trailing dot, no www.


@dataclass(frozen=True)
class Attribution:
    """One source attribution result."""

    entity_snapshot_id: str
    source_host: str
    attribution_type: str = "OWNED_DOMAIN"


@dataclass(frozen=True)
class SourceAttributionDecision:
    """Typed result of attributing a single source host.

    Distinguishes NO_MATCH (no tracked domain matched), ATTRIBUTED
    (exactly one entity matched), and AMBIGUOUS (multiple equally-
    specific domains matched — no attribution, caller should warn).
    """

    outcome: AttributionOutcome
    attribution: Attribution | None = None


@dataclass
class AttributionResult:
    """Result of attributing sources for one response."""

    attributions: list[Attribution] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)


def _normalize_domain(domain: str) -> str:
    """Normalize a domain: lowercase, strip trailing dot, strip www. prefix."""
    d = unicodedata.normalize("NFKC", domain.strip()).lower().rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return d


def parse_source_host(url: str) -> str | None:
    """Extract a normalized hostname from a source URL.

    Returns None if the URL is invalid or has no hostname.
    Does NOT fetch the URL, resolve redirects, or perform DNS.
    Does NOT modify the stored ResponseSource.url.
    """
    if not url or not url.strip():
        return None
    url = url.strip()
    try:
        # Add a scheme if missing so urlparse can extract the host.
        if "://" not in url:
            parsed = urllib.parse.urlparse("https://" + url)
        else:
            parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if host is None:
            return None
        host = host.lower().rstrip(".")
        if not host:
            return None
        # Reject hosts that don't look like domains (must contain a dot
        # and only valid hostname characters). This filters out text
        # that urlparse misinterprets as a hostname.
        if "." not in host:
            return None
        if not host.replace(".", "").replace("-", "").isalnum():
            return None
        # IDN-safe: convert to NFKC for consistent comparison.
        host = unicodedata.normalize("NFKC", host)
        return host
    except (ValueError, TypeError):
        return None


def _domain_matches(host: str, domain: str) -> bool:
    """Check if host matches domain exactly or as a subdomain.

    ``host == domain`` or ``host`` ends with ``"." + domain``.
    NOT naive substring/suffix matching.
    """
    if host == domain:
        return True
    return bool(host.endswith("." + domain))


def _specificity(domain: str) -> int:
    """Return the specificity of a domain (number of labels).

    ``acme.com`` → 2
    ``product.example.com`` → 3
    """
    return len([label for label in domain.split(".") if label])


def build_entity_domains(snapshots: list[dict[str, Any]]) -> list[EntityDomain]:
    """Build entity domain list from snapshot dicts.

    Each snapshot dict must have: entity_snapshot_id (str), domain (str).
    """
    domains: list[EntityDomain] = []
    seen: set[tuple[str, str]] = set()
    for snap in snapshots:
        eid = snap["entity_snapshot_id"]
        domain = _normalize_domain(snap["domain"])
        if not domain:
            continue
        key = (eid, domain)
        if key in seen:
            continue
        seen.add(key)
        domains.append(EntityDomain(entity_snapshot_id=eid, domain=domain))
    return domains


def attribute_source(
    source_host: str,
    entity_domains: list[EntityDomain],
) -> SourceAttributionDecision:
    """Attribute a single source host to the most-specific matching entity.

    Returns a SourceAttributionDecision with outcome:
    - NO_MATCH: no tracked domain matched.
    - ATTRIBUTED: exactly one entity matched (most-specific wins).
    - AMBIGUOUS: multiple equally-specific domains matched; no
      attribution is produced. The caller should record an
      AMBIGUOUS_SOURCE_DOMAIN warning.
    """
    if not source_host:
        return SourceAttributionDecision(outcome=AttributionOutcome.NO_MATCH)

    matching: list[EntityDomain] = []
    for ed in entity_domains:
        if _domain_matches(source_host, ed.domain):
            matching.append(ed)

    if not matching:
        return SourceAttributionDecision(outcome=AttributionOutcome.NO_MATCH)

    if len(matching) == 1:
        ed = matching[0]
        return SourceAttributionDecision(
            outcome=AttributionOutcome.ATTRIBUTED,
            attribution=Attribution(
                entity_snapshot_id=ed.entity_snapshot_id,
                source_host=source_host,
            ),
        )

    # Multiple matches: find the most specific (most labels).
    max_spec = max(_specificity(ed.domain) for ed in matching)
    most_specific = [ed for ed in matching if _specificity(ed.domain) == max_spec]

    if len(most_specific) == 1:
        ed = most_specific[0]
        return SourceAttributionDecision(
            outcome=AttributionOutcome.ATTRIBUTED,
            attribution=Attribution(
                entity_snapshot_id=ed.entity_snapshot_id,
                source_host=source_host,
            ),
        )

    # Ambiguous: equally specific domains match. Don't attribute.
    return SourceAttributionDecision(outcome=AttributionOutcome.AMBIGUOUS)


def attribute_sources(
    sources: list[tuple[str, str]],
    entity_domains: list[EntityDomain],
) -> AttributionResult:
    """Attribute multiple sources to entities.

    Each source is a tuple of (source_id, url).
    Returns attributions and warnings for invalid/ambiguous sources.
    """
    result = AttributionResult()

    for _source_id, url in sources:
        host = parse_source_host(url)
        if host is None:
            result.warnings.append(
                (
                    WarningCode.INVALID_SOURCE_URL.value,
                    f"Could not parse hostname from source URL: {url[:100]}",
                )
            )
            continue

        decision = attribute_source(host, entity_domains)
        if decision.outcome == AttributionOutcome.ATTRIBUTED and decision.attribution:
            result.attributions.append(decision.attribution)
        elif decision.outcome == AttributionOutcome.AMBIGUOUS:
            result.warnings.append(
                (
                    WarningCode.AMBIGUOUS_SOURCE_DOMAIN.value,
                    f"Source host '{host}' matches multiple equally-specific tracked domains; "
                    "no attribution assigned.",
                )
            )
        # NO_MATCH: the source simply has no attribution — not a warning.

    return result

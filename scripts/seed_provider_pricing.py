#!/usr/bin/env python3
"""Bootstrap verified provider pricing evidence for production scans.

Creates immutable ProviderPriceRule rows with pinned, reviewable pricing
data.  This operator makes ZERO provider/network calls — all pricing
evidence is hardcoded from independently verified official documentation.

Idempotency:
    - If the exact pricing_key exists and every canonical field matches:
      report ALREADY PRESENT, make zero writes.
    - If the same pricing_key exists but canonical values differ:
      FAIL CLOSED, print a sanitized conflict, make zero writes.
    - If an overlapping effective rule exists for the same
      provider/surface/model: FAIL CLOSED, make zero writes.

Modes:
    --check   Read-only: report MISSING, READY, or CONFLICT.
    --apply   Mutate: create the rule if missing and no conflicts.

Usage:
    python -m scripts.seed_provider_pricing --check
    python -m scripts.seed_provider_pricing --apply

Environment:
    DATABASE_URL must point to the production database.
    APP_ENV must be production (or staging).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

EXIT_OK = 0
EXIT_FAIL = 1

# Status codes for --check mode.
STATUS_MISSING = "MISSING"
STATUS_READY = "READY"
STATUS_CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class PinnedPriceRule:
    """Pinned, reviewable pricing evidence for one provider/surface/model.

    All monetary values are Decimal (never binary floats).
    """

    pricing_key: str
    provider: str
    provider_surface: str
    model: str
    effective_from: datetime
    effective_to: datetime | None
    input_per_million_usd: Decimal | None
    cached_input_per_million_usd: Decimal | None
    cache_write_per_million_usd: Decimal | None
    output_per_million_usd: Decimal | None
    reasoning_per_million_usd: Decimal | None
    citation_per_million_usd: Decimal | None
    search_per_1000_usd: Decimal | None
    request_fee_usd: Decimal | None
    input_tokens_include_cached: bool
    output_tokens_include_reasoning: bool
    verified_at: datetime
    source_url: str
    notes: str


# ---------------------------------------------------------------------------
# Pinned pricing evidence — verified 2026-09-05 against official OpenAI docs.
#   Model page:  https://developers.openai.com/api/docs/models/gpt-5.6-terra
#   Pricing page: https://openai.com/api/pricing/
#
# Cache writes documented at 1.25x uncached input: 2.00 * 1.25 = 2.50
# Reasoning tokens are part of billed output for this model.
# Web search rate: $10.00 per 1000 searches.
# Effective date for current Terra price: 2026-07-30T00:00:00Z
# ---------------------------------------------------------------------------
_OPENAI_GPT56_TERRA = PinnedPriceRule(
    pricing_key="openai:responses:gpt-5.6-terra:2026-07-30",
    provider="OPENAI",
    provider_surface="OPENAI_RESPONSES_API",
    model="gpt-5.6-terra",
    effective_from=datetime(2026, 7, 30, 0, 0, 0, tzinfo=UTC),
    effective_to=None,
    input_per_million_usd=Decimal("2.00"),
    cached_input_per_million_usd=Decimal("0.20"),
    cache_write_per_million_usd=Decimal("2.50"),
    output_per_million_usd=Decimal("12.00"),
    reasoning_per_million_usd=Decimal("12.00"),
    citation_per_million_usd=None,
    search_per_1000_usd=Decimal("10.00"),
    request_fee_usd=None,
    input_tokens_include_cached=True,
    output_tokens_include_reasoning=True,
    verified_at=datetime(2026, 9, 5, 0, 0, 0, tzinfo=UTC),
    source_url="https://developers.openai.com/api/docs/models/gpt-5.6-terra",
    notes=(
        "Verified 2026-09-05 against official OpenAI documentation. "
        "Standard API pricing for gpt-5.6-terra via the Responses API. "
        "Cache writes at 1.25x uncached input (2.00 * 1.25 = 2.50). "
        "Reasoning tokens are part of billed output; "
        "output_tokens_include_reasoning=true with a separate "
        "reasoning_per_million_usd rate so the calculator subtracts "
        "reasoning from output to avoid double billing. "
        "Web search: $10.00 per 1000 search requests. "
        "Effective 2026-07-30T00:00:00Z."
    ),
)

# All pinned rules known to this operator.
_PINNED_RULES: list[PinnedPriceRule] = [_OPENAI_GPT56_TERRA]

# Canonical fields used for conflict detection.  These are the monetary
# and semantic fields that define a pricing rule's identity.  Changes to
# these fields on an existing pricing_key constitute a conflict.
_CANONICAL_FIELDS = (
    "provider",
    "provider_surface",
    "model",
    "effective_from",
    "effective_to",
    "input_per_million_usd",
    "cached_input_per_million_usd",
    "cache_write_per_million_usd",
    "output_per_million_usd",
    "reasoning_per_million_usd",
    "citation_per_million_usd",
    "search_per_1000_usd",
    "request_fee_usd",
    "input_tokens_include_cached",
    "output_tokens_include_reasoning",
)


def _rule_to_dict(rule: PinnedPriceRule) -> dict[str, object]:
    """Convert a PinnedPriceRule to a dict of canonical fields."""
    return {field: getattr(rule, field) for field in _CANONICAL_FIELDS}


def _existing_to_dict(rule: object) -> dict[str, object]:
    """Extract canonical fields from a persisted ProviderPriceRule."""
    return {field: getattr(rule, field) for field in _CANONICAL_FIELDS}


def _values_match(
    pinned: dict[str, object],
    existing: dict[str, object],
) -> bool:
    """Check if all canonical fields match between pinned and existing."""
    for field in _CANONICAL_FIELDS:
        pv = pinned[field]
        ev = existing[field]
        # Normalize Decimal comparisons (DB may return Decimal with
        # different internal precision but same value).
        if isinstance(pv, Decimal) and isinstance(ev, Decimal):
            if pv != ev:
                return False
        elif pv != ev:
            return False
    return True


def _format_conflict(
    pinned: PinnedPriceRule,
    existing: object,
) -> str:
    """Produce a sanitized conflict report (no secrets)."""
    pd = _rule_to_dict(pinned)
    ed = _existing_to_dict(existing)
    diffs: list[str] = []
    for field in _CANONICAL_FIELDS:
        pv = pd[field]
        ev = ed[field]
        if isinstance(pv, Decimal) and isinstance(ev, Decimal):
            if pv != ev:
                diffs.append(f"  {field}: pinned={pv} existing={ev}")
        elif pv != ev:
            diffs.append(f"  {field}: pinned={pv!r} existing={ev!r}")
    return (
        f"CONFLICT: pricing_key '{pinned.pricing_key}' exists with "
        f"different canonical values:\n" + "\n".join(diffs)
        if diffs
        else f"CONFLICT: pricing_key '{pinned.pricing_key}' mismatch"
    )


def _check_overlapping(
    session: object,
    pinned: PinnedPriceRule,
) -> str | None:
    """Check for overlapping effective rules for the same provider/surface/model.

    Returns a conflict message if an overlap is found, None otherwise.
    Excludes the exact pricing_key (that's handled separately).
    """
    from sqlalchemy import or_, select

    from app.models.pricing import ProviderPriceRule

    overlapping = (
        session.execute(
            select(ProviderPriceRule).where(
                ProviderPriceRule.provider == pinned.provider,
                ProviderPriceRule.provider_surface == pinned.provider_surface,
                ProviderPriceRule.model == pinned.model,
                ProviderPriceRule.pricing_key != pinned.pricing_key,
                # Overlap: existing.effective_from < pinned.effective_to
                #          AND existing.effective_to > pinned.effective_from
                # If pinned has no effective_to, it's open-ended.
                ProviderPriceRule.effective_from
                < (
                    pinned.effective_to
                    if pinned.effective_to is not None
                    else datetime(9999, 12, 31, tzinfo=UTC)
                ),
                or_(
                    ProviderPriceRule.effective_to.is_(None),
                    ProviderPriceRule.effective_to > pinned.effective_from,
                ),
            )
        )
        .scalars()
        .all()
    )

    if overlapping:
        keys = [r.pricing_key for r in overlapping]
        return (
            f"CONFLICT: overlapping rule(s) for "
            f"{pinned.provider}/{pinned.provider_surface}/{pinned.model}: "
            f"{', '.join(keys)}"
        )
    return None


def _check_one(session: object, pinned: PinnedPriceRule) -> str:
    """Check the status of one pinned rule.  Returns MISSING/READY/CONFLICT."""
    from sqlalchemy import select

    from app.models.pricing import ProviderPriceRule

    existing = session.execute(
        select(ProviderPriceRule).where(ProviderPriceRule.pricing_key == pinned.pricing_key)
    ).scalar_one_or_none()

    if existing is None:
        # Check for overlapping rules before declaring MISSING (safe to create).
        overlap = _check_overlapping(session, pinned)
        if overlap:
            return STATUS_CONFLICT
        return STATUS_MISSING

    if _values_match(_rule_to_dict(pinned), _existing_to_dict(existing)):
        return STATUS_READY

    return STATUS_CONFLICT


def _apply_one(session: object, pinned: PinnedPriceRule) -> tuple[str, str | None]:
    """Apply one pinned rule.

    Returns (status, conflict_message).
    status is one of: CREATED, READY, CONFLICT.
    """
    from sqlalchemy import select

    from app.models.pricing import ProviderPriceRule

    existing = session.execute(
        select(ProviderPriceRule).where(ProviderPriceRule.pricing_key == pinned.pricing_key)
    ).scalar_one_or_none()

    if existing is not None:
        if _values_match(_rule_to_dict(pinned), _existing_to_dict(existing)):
            return ("READY", None)
        return ("CONFLICT", _format_conflict(pinned, existing))

    # No existing rule with this pricing_key — check for overlaps.
    overlap = _check_overlapping(session, pinned)
    if overlap:
        return ("CONFLICT", overlap)

    # Create the rule.
    rule = ProviderPriceRule(
        pricing_key=pinned.pricing_key,
        provider=pinned.provider,
        provider_surface=pinned.provider_surface,
        model=pinned.model,
        effective_from=pinned.effective_from,
        effective_to=pinned.effective_to,
        input_per_million_usd=pinned.input_per_million_usd,
        cached_input_per_million_usd=pinned.cached_input_per_million_usd,
        cache_write_per_million_usd=pinned.cache_write_per_million_usd,
        output_per_million_usd=pinned.output_per_million_usd,
        reasoning_per_million_usd=pinned.reasoning_per_million_usd,
        citation_per_million_usd=pinned.citation_per_million_usd,
        search_per_1000_usd=pinned.search_per_1000_usd,
        request_fee_usd=pinned.request_fee_usd,
        input_tokens_include_cached=pinned.input_tokens_include_cached,
        output_tokens_include_reasoning=pinned.output_tokens_include_reasoning,
        verified_at=pinned.verified_at,
        source_url=pinned.source_url,
        notes=pinned.notes,
    )
    session.add(rule)
    session.flush()
    return ("CREATED", None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap verified provider pricing evidence.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="Read-only: report MISSING/READY/CONFLICT for each pinned rule.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Create missing rules.  Fails closed on conflict.  Idempotent.",
    )
    args = parser.parse_args(argv)

    from app.db.session import get_session_factory

    factory = get_session_factory()

    if args.check:
        # Read-only mode: zero writes.
        with factory() as session:
            for pinned in _PINNED_RULES:
                status = _check_one(session, pinned)
                print(f"{pinned.pricing_key}: {status}")
                if status == STATUS_CONFLICT:
                    # Report detail for conflict.
                    existing = session.execute(
                        _select_by_key(pinned.pricing_key)
                    ).scalar_one_or_none()
                    if existing is not None:
                        print(_format_conflict(pinned, existing))
                    else:
                        overlap = _check_overlapping(session, pinned)
                        if overlap:
                            print(overlap)
                    print("ERROR: Pricing conflict detected. Resolve before proceeding.")
                    return EXIT_FAIL
        print("OK: check complete")
        return EXIT_OK

    if args.apply:
        created = 0
        ready = 0
        conflicts = 0
        with factory() as session:
            for pinned in _PINNED_RULES:
                status, msg = _apply_one(session, pinned)
                if status == "CREATED":
                    created += 1
                    print(f"Created: {pinned.pricing_key}")
                elif status == "READY":
                    ready += 1
                    print(f"Already present: {pinned.pricing_key}")
                elif status == "CONFLICT":
                    conflicts += 1
                    if msg:
                        print(msg)
            if conflicts:
                session.rollback()
                print(
                    f"\nFAILED: {conflicts} conflict(s), "
                    f"{created} created, {ready} already present. "
                    f"Zero writes committed."
                )
                return EXIT_FAIL
            session.commit()
        print(f"\nOK: {created} created, {ready} already present.")
        return EXIT_OK

    # Should not reach here (mutually exclusive required group).
    return EXIT_FAIL


def _select_by_key(pricing_key: str):
    """Build a select for ProviderPriceRule by pricing_key."""
    from sqlalchemy import select

    from app.models.pricing import ProviderPriceRule

    return select(ProviderPriceRule).where(ProviderPriceRule.pricing_key == pricing_key)


if __name__ == "__main__":
    sys.exit(main())

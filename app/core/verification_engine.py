"""Phase 10 Verification Engine constants.

All thresholds and the methodology version identifier for deterministic
opportunity verification live here. Do not scatter magic numbers across
services.

Future methodology changes require a version bump.
"""

from __future__ import annotations

from decimal import Decimal

# --- Methodology version ---
VERIFICATION_METHODOLOGY_VERSION = "opportunity-verification-v1"

# --- Coverage gates ---
# Both baseline and verification measurement coverage must meet this
# threshold (as a percentage) for the comparison to be considered
# reliable. Below this, the outcome is INCONCLUSIVE.
MIN_VERIFICATION_COVERAGE_PCT = Decimal("75")

# --- Meaningful improvement thresholds (in percentage points) ---
# A change smaller than this is NOT_IMPROVED rather than IMPROVED or
# REGRESSED.
MIN_VISIBILITY_IMPROVEMENT_PP = Decimal("5")
MIN_CITATION_IMPROVEMENT_PP = Decimal("5")
MIN_PROMPT_GAP_IMPROVEMENT_PP = Decimal("10")

# --- Resolution thresholds ---
# These reuse the Action Engine trigger thresholds so that "RESOLVED"
# means "the issue no longer meets the criterion that created the
# Opportunity."  Imported lazily to avoid circular imports at module
# load time.

# Re-exported from action_engine for convenience.
from app.core.action_engine import (  # noqa: E402
    MIN_CITATION_ELIGIBLE_OBSERVATIONS,
    MIN_DISCOVERY_VISIBILITY_GAP_PP,
    MIN_OWNED_CITATION_GAP_PP,
    MIN_PROVIDER_VISIBILITY_GAP_PP,
)

__all__ = [
    "MIN_CITATION_ELIGIBLE_OBSERVATIONS",
    "MIN_CITATION_IMPROVEMENT_PP",
    "MIN_DISCOVERY_VISIBILITY_GAP_PP",
    "MIN_OWNED_CITATION_GAP_PP",
    "MIN_PROMPT_GAP_IMPROVEMENT_PP",
    "MIN_PROVIDER_VISIBILITY_GAP_PP",
    "MIN_VERIFICATION_COVERAGE_PCT",
    "MIN_VISIBILITY_IMPROVEMENT_PP",
    "VERIFICATION_METHODOLOGY_VERSION",
]

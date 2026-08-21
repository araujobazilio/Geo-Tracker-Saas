"""Phase 9 Action Engine constants.

All thresholds and version identifiers for deterministic action
generation live here. Do not scatter magic numbers across services.

If rules or thresholds change materially, bump ACTION_ENGINE_VERSION.
"""

from __future__ import annotations

from decimal import Decimal

# --- Methodology version ---
# v1.1: citation eligibility minimum enforced, global SOV formula,
# prompt-run evidence lineage, concurrent refresh safety.
ACTION_ENGINE_VERSION = "deterministic-actions-v1.1"

# --- Rule 1: Discovery visibility gap (NON_BRANDED, per competitor) ---
MIN_GLOBAL_SUCCESSFUL_OBSERVATIONS = 3
MIN_DISCOVERY_VISIBILITY_GAP_PP = Decimal("10")
HIGH_DISCOVERY_VISIBILITY_GAP_PP = Decimal("25")

# --- Rule 2: Provider visibility gap (per provider, NON_BRANDED) ---
MIN_PROVIDER_SUCCESSFUL_OBSERVATIONS = 2
MIN_PROVIDER_VISIBILITY_GAP_PP = Decimal("15")
HIGH_PROVIDER_VISIBILITY_GAP_PP = Decimal("30")

# --- Rule 3: Owned citation gap ---
MIN_CITATION_ELIGIBLE_OBSERVATIONS = 2
MIN_OWNED_CITATION_GAP_PP = Decimal("20")

# --- Rule 4: Prompt competitor gap ---
MAX_PROMPT_OPPORTUNITIES_PER_COMPETITOR = 5

# --- Global safety: avoid flooding the Action Center ---
MAX_OPPORTUNITIES_PER_REFRESH = 50

"""Phase 10.1 — Verification Scope Resolver.

Determines the exact historical baseline PromptRun cells that must be
re-measured for a specific Opportunity.  The same scope drives BOTH
the provider execution plan AND the baseline/verification evaluation.

A target cell represents the exact historical baseline cell:

    prompt_id
    provider
    provider_surface
    execution_mode
    requested_model

The resolver does NOT derive a provider-wide target and then rebuild
a Cartesian product.  It copies exact selected baseline PromptRun
cells, preserving non-rectangular plans.

Scope rules by OpportunityType:

DISCOVERY_VISIBILITY_GAP:
    Select baseline PromptRun cells where Prompt.prompt_type == NON_BRANDED
    across all providers represented by those selected historical cells.

PROVIDER_VISIBILITY_GAP:
    Select baseline PromptRun cells where Prompt.prompt_type == NON_BRANDED
    AND PromptRun.provider == Opportunity.provider.

OWNED_CITATION_GAP:
    Select baseline PromptRun cells where Prompt.prompt_type == NON_BRANDED
    AND PromptRun.execution_mode == WEB_GROUNDED.

PROMPT_COMPETITOR_GAP:
    Select baseline PromptRun cells where PromptRun.prompt_id == Opportunity.prompt_id
    across historical provider targets for that exact prompt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    LLMProvider,
    OpportunityType,
    PromptType,
    ProviderExecutionMode,
    ProviderSurface,
)
from app.models.opportunity import Opportunity
from app.models.scan import PromptRun, Scan
from app.models.tracking import Prompt


@dataclass(frozen=True)
class VerificationTargetCell:
    """One exact historical baseline cell to re-measure.

    Uniquely identified by (prompt_id, provider) plus its snapshotted
    surface, mode, and requested_model.
    """

    prompt_id: uuid.UUID
    provider: LLMProvider
    provider_surface: ProviderSurface
    execution_mode: ProviderExecutionMode
    requested_model: str


@dataclass(frozen=True)
class VerificationScope:
    """The resolved set of target cells for a verification scan.

    Attributes:
        target_cells: exact historical baseline cells to re-measure.
        prompt_ids: unique prompt IDs in the target cells (for prompt_count).
        providers: unique providers in the target cells (for provider_count).
    """

    target_cells: tuple[VerificationTargetCell, ...]
    prompt_ids: frozenset[uuid.UUID]
    providers: frozenset[LLMProvider]

    @property
    def planned_ai_checks(self) -> int:
        """Number of exact target cells — NOT prompt_count * provider_count."""
        return len(self.target_cells)

    @property
    def prompt_count(self) -> int:
        return len(self.prompt_ids)

    @property
    def provider_count(self) -> int:
        return len(self.providers)


class VerificationScopeResolver:
    """Resolve the exact historical baseline cells for an Opportunity.

    The same scope drives BOTH the provider execution plan AND the
    baseline/verification evaluation.  This ensures that the evaluation
    compares corresponding methodological cells, not a broader scope.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def resolve(
        self,
        opportunity: Opportunity,
        baseline_scan: Scan,
    ) -> VerificationScope:
        """Resolve the verification scope for the given Opportunity.

        Returns the exact set of historical baseline PromptRun cells
        that must be re-measured.

        Raises ValidationError if no cells match the scope criteria.
        """
        opp_type = opportunity.opportunity_type

        # Load all baseline PromptRuns joined with their Prompts.
        baseline_runs = list(
            self._session.execute(
                select(PromptRun).where(PromptRun.scan_id == baseline_scan.id)
            ).scalars()
        )

        prompt_cache: dict[uuid.UUID, Prompt] = {}
        for run in baseline_runs:
            if run.prompt_id not in prompt_cache:
                prompt = self._session.get(Prompt, run.prompt_id)
                if prompt is not None:
                    prompt_cache[run.prompt_id] = prompt

        # Filter cells based on Opportunity type.
        cells: list[VerificationTargetCell] = []
        seen: set[tuple[uuid.UUID, LLMProvider]] = set()

        for run in baseline_runs:
            prompt = prompt_cache.get(run.prompt_id)
            if prompt is None:
                continue

            if not self._cell_in_scope(opportunity, run, prompt):
                continue

            cell_key = (run.prompt_id, run.provider)
            if cell_key in seen:
                continue
            seen.add(cell_key)

            cells.append(
                VerificationTargetCell(
                    prompt_id=run.prompt_id,
                    provider=run.provider,
                    provider_surface=run.provider_surface,
                    execution_mode=run.execution_mode,
                    requested_model=run.requested_model,
                )
            )

        if not cells:
            from app.core.exceptions import ValidationError

            raise ValidationError(
                f"No baseline PromptRun cells match the verification scope "
                f"for opportunity type {opp_type.value}."
            )

        prompt_ids = frozenset(c.prompt_id for c in cells)
        providers = frozenset(c.provider for c in cells)

        return VerificationScope(
            target_cells=tuple(cells),
            prompt_ids=prompt_ids,
            providers=providers,
        )

    def _cell_in_scope(
        self,
        opportunity: Opportunity,
        run: PromptRun,
        prompt: Prompt,
    ) -> bool:
        """Determine whether a baseline PromptRun cell is in the
        verification scope for this Opportunity.
        """
        opp_type = opportunity.opportunity_type

        if opp_type == OpportunityType.DISCOVERY_VISIBILITY_GAP:
            # NON_BRANDED prompts across all providers.
            return prompt.prompt_type == PromptType.NON_BRANDED

        if opp_type == OpportunityType.PROVIDER_VISIBILITY_GAP:
            # NON_BRANDED prompts for the Opportunity's provider only.
            return (
                prompt.prompt_type == PromptType.NON_BRANDED
                and run.provider == opportunity.provider
            )

        if opp_type == OpportunityType.OWNED_CITATION_GAP:
            # NON_BRANDED prompts with WEB_GROUNDED execution mode only.
            return (
                prompt.prompt_type == PromptType.NON_BRANDED
                and run.execution_mode == ProviderExecutionMode.WEB_GROUNDED
            )

        if opp_type == OpportunityType.PROMPT_COMPETITOR_GAP:
            # Exact prompt_id only, across all providers for that prompt.
            return (
                opportunity.prompt_id is not None
                and run.prompt_id == opportunity.prompt_id
            )

        # Fallback: include all NON_BRANDED cells.
        return prompt.prompt_type == PromptType.NON_BRANDED

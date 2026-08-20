# Competitor Explanations

## Status

**IMPLEMENTED (Phase 9).** Deterministic, evidence-based
brand-vs-competitor explanations are computed from persisted Scan
analysis evidence without any AI analysis, provider API calls, or AI
Check consumption.

## Overview

The `CompetitorExplanationService` computes deterministic explanations
comparing a brand vs a single competitor from persisted Scan analysis
evidence. Each explanation surfaces the observed visibility gap, share
of voice, citation gap, per-provider breakdown, prompt-level gaps, and
optional reliability context — all derived from immutable persisted
evidence.

The service produces two views:

1. **Competitor summaries** — one compact summary per COMPETITOR
   snapshot in a scan (visibility rates, gaps, citation rates,
   competitor-only runs, optional reliability context).
2. **Detailed explanation** — full evidence for a single
   brand-vs-competitor pair (overlap matrix, provider breakdown, prompt
   gaps, owned citation evidence, reliability context).

## Key principles

- **Zero AI Checks, zero provider calls, zero UsageEvents.** The
  explanation operates entirely on persisted evidence.
- **Uses ONLY persisted evidence.** Inputs are `PromptRun`,
  `EntityMention`, `SourceAttribution`, and `ScanEntitySnapshot` rows
  produced by the deterministic analysis (Phase 7).
- **Requires COMPLETED analysis (fail closed).** A missing, `PENDING`,
  `RUNNING`, or `FAILED` `ScanAnalysis` is NOT evidence that a brand
  was absent. The service raises `ConflictError("Scan analysis is not
  completed.")` instead of returning a false zero.
- **Uses historical `ScanEntitySnapshot`, not current mutable Project
  state.** Entity names and domains come from the immutable snapshot
  captured at scan time, so explanations reflect what was true when the
  scan ran — not later edits to `Project` or `Competitor` records.
- **No causal claims.** Explanations describe observed gaps and
  measured differences only. They do not assert why a gap exists or
  what would close it.
- **Optional Confidence context.** When a linked `CONFIDENCE` scan
  exists, reliability metrics (overall visibility rate, mention
  stability, repeat sufficiency, observed min/max, confidence level)
  are attached as context — never as a causal explanation.

## Metrics computed

All rates are computed as `Decimal` percentages with 4 decimal places
(`ROUND_HALF_UP`). A rate is `None` when its denominator is zero (no
observations to measure).

### Brand / competitor visibility rates

```
visibility_rate = mentioned_runs / successful_observations * 100
```

- `successful_observations`: SUCCEEDED `PromptRun` rows matching the
  selected `prompt_type` (and optional `provider` filter).
- `mentioned_runs`: SUCCEEDED runs where the entity has at least one
  `EntityMention`.
- **Range**: [0.0, 100.0]
- **Null semantics**: `successful_observations == 0` → `None` (no
  measurement).
- **Zero semantics**: `successful_observations > 0` and
  `mentioned_runs == 0` → `0.0000` (true measured zero).

### Visibility gap

```
visibility_gap_pp = competitor_visibility_rate - brand_visibility_rate
```

- Expressed in percentage points (pp).
- `None` if either rate is `None`.
- Positive values indicate the competitor was more visible; negative
  values indicate the brand was more visible.

### Share of voice

```
share_of_voice = entity_mentioned_runs / total_mentioned_presences * 100
```

- `total_mentioned_presences` = `brand_mentioned_runs` +
  `competitor_mentioned_runs`.
- `None` when `total_mentioned_presences == 0`.
- **Range**: [0.0, 100.0]

### Overlap matrix

SUCCEEDED runs are classified into four mutually exclusive buckets
based on brand/competitor mention presence:

| Bucket | Meaning |
|--------|---------|
| `brand_only_runs` | Brand mentioned, competitor not mentioned |
| `competitor_only_runs` | Competitor mentioned, brand not mentioned |
| `both_runs` | Both brand and competitor mentioned |
| `neither_runs` | Neither brand nor competitor mentioned |

```
brand_only + competitor_only + both + neither == successful_observations
```

The matrix MUST reconcile to `successful_observations`. The
`competitor_only_rate` is computed as
`competitor_only_runs / successful_observations * 100`.

### Provider breakdown

For each provider that ran prompts for the scan (filtered by
`prompt_type`):

| Field | Description |
|-------|-------------|
| `provider` | LLM provider name |
| `planned_observations` | Total runs for this provider and prompt type |
| `successful_observations` | SUCCEEDED runs for this provider |
| `measurement_coverage` | `successful / planned * 100` |
| `brand_visibility_rate` | Brand mentioned runs / successful * 100 |
| `competitor_visibility_rate` | Competitor mentioned runs / successful * 100 |
| `visibility_gap_pp` | `competitor_visibility_rate - brand_visibility_rate` |
| `brand_owned_citation_rate` | Brand cited runs / WEB_GROUNDED runs * 100 |
| `competitor_owned_citation_rate` | Competitor cited runs / WEB_GROUNDED runs * 100 |
| `competitor_only_runs` | Runs where competitor mentioned and brand not |

Providers are sorted by name ascending.

### Owned citation rates

Citation metrics are computed only over `WEB_GROUNDED` runs (runs with
`execution_mode == ProviderExecutionMode.WEB_GROUNDED`), since only
those produce source URLs:

```
owned_citation_rate = cited_runs / web_grounded_runs * 100
```

- `web_grounded_runs`: SUCCEEDED `WEB_GROUNDED` runs in scope.
- `cited_runs`: runs with at least one `SourceAttribution` attributed
  to the entity (owned-domain match).
- `None` when `web_grounded_runs == 0`.

### Citation gap

```
citation_gap_pp = competitor_owned_citation_rate - brand_owned_citation_rate
```

- Expressed in percentage points (pp).
- `None` if either rate is `None`.

### Prompt gaps

Prompts where the competitor appears and the brand does not. For each
gap:

| Field | Description |
|-------|-------------|
| `prompt_id` | The prompt UUID |
| `prompt_text` | The prompt text |
| `prompt_type` | The prompt type |
| `intent` | Prompt intent (if set) |
| `funnel_stage` | Funnel stage (if set) |
| `commercial_intent` | Whether the prompt has commercial intent |
| `affected_providers` | Providers that produced competitor-only runs |
| `successful_observations` | Total successful runs for this prompt |
| `competitor_only_count` | Runs where competitor mentioned, brand not |

Prompt gaps are sorted by priority:

1. `PURCHASE` funnel stage before `CONSIDERATION` before `AWARENESS`.
2. Commercial-intent prompts before non-commercial.
3. More affected providers first.
4. Prompt ID ascending as a tiebreaker.

### Reliability context (optional)

When a linked `CONFIDENCE` scan exists (a `CONFIDENCE` scan whose
`baseline_scan_id` points to this `STANDARD` scan), the service loads
brand reliability metrics from the most recent completed confidence
analysis. The context is matched by `entity_key` (not snapshot UUID,
since confidence scans have their own snapshots).

| Field | Description |
|-------|-------------|
| `confidence_scan_id` | The linked CONFIDENCE scan UUID |
| `overall_visibility_rate` | Brand visibility across all observations |
| `mention_stability` | Consistency of brand mentions across repeats |
| `repeat_sufficiency` | Succeeded observations / planned repeats |
| `observed_visibility_min` | Lowest visibility rate across observations |
| `observed_visibility_max` | Highest visibility rate across observations |
| `confidence_level` | Heuristic level (`INSUFFICIENT`/`LOW`/`MEDIUM`/`HIGH`) |
| `confidence_methodology_version` | Methodology version string |

If no linked confidence scan exists, or none has a completed analysis,
`reliability_context` is `None`.

## Historical snapshot integrity

The service uses `ScanEntitySnapshot` rows — immutable copies of entity
names and domains captured at scan time. It does NOT read current
`Project` or `Competitor` state. This ensures explanations reflect what
was true when the scan ran, even if the project's brand or competitors
were later renamed, added, or removed.

The brand snapshot is identified by `TrackedEntityType.BRAND`; the
competitor snapshot is identified by `TrackedEntityType.COMPETITOR`. A
snapshot requested as a competitor that is not of type `COMPETITOR` is
rejected with `ValidationError`.

## True measured zero vs NULL

A `0.0000` visibility rate with a completed analysis is a **true
measured zero** — the entity was measured but never mentioned. This is
distinct from `None`, which means no measurement was possible (zero
successful observations).

| Scenario | `visibility_rate` | Behavior |
|----------|-------------------|----------|
| COMPLETED analysis, 0 mentions | `0.0000` | True measured zero |
| COMPLETED analysis, 0 successful obs | `None` | No measurement |
| Missing/FAILED/PENDING/RUNNING analysis | N/A | `ConflictError` |

This mirrors the Phase 8.1 fail-closed semantics enforced by
`VisibilityMetricsService` and `ConfidenceMetricsService`.

## API endpoints

All endpoints are mounted under
`/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans`.

| Method | Path | Role | Description |
|--------|------|------|-------------|
| GET | `/{scan_id}/competitors` | MEMBER | List evidence-based summaries for all competitors |
| GET | `/{scan_id}/competitors/{entity_snapshot_id}/explanation` | MEMBER | Get detailed explanation for a specific competitor |

### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt_type` | `PromptType` | `NON_BRANDED` | Filter runs by prompt type |
| `provider` | `LLMProvider` | `None` | Filter runs by provider (explanation endpoint only) |

### Role matrix

| Role | Permission |
|------|------------|
| `OWNER` | Read explanations |
| `ADMIN` | Read explanations |
| `MEMBER` | Read explanations |

Membership is enforced via `WorkspaceAuthorizationService.require_membership`.

### Tenant isolation

All endpoints filter by `workspace_id` and `project_id`. Cross-tenant
access (scan not found in the caller's workspace/project) returns 404
(`NotFoundError`).

### Analysis readiness

Both endpoints require a `COMPLETED` `ScanAnalysis` for the scan. A
missing, `PENDING`, `RUNNING`, or `FAILED` analysis raises
`ConflictError("Scan analysis is not completed.")` — the service fails
closed rather than returning a false zero.

The scan must also be terminal (`COMPLETED` or `PARTIAL`) and of type
`STANDARD`. Non-terminal scans raise `ConflictError`; non-standard
scans raise `ValidationError`.

## No causal claims

Explanations describe **observed patterns**, not causation. The
service reports:

- Where the competitor was mentioned and the brand was not (prompt
  gaps).
- Where the competitor received owned-domain citations and the brand
  did not (citation evidence).
- How visibility and citation rates differ (measured gaps).

It does NOT assert why these gaps exist or recommend specific actions.
Causal interpretation and recommended actions are handled by the
Action Engine (Phase 9 Opportunity detection), which is documented
separately.

## Determinism

All explanations are computed from persisted evidence. The same
analysis evidence always produces the same explanation. No randomness,
no AI, no external calls.

## See also

- `docs/DETECTION_ENGINE.md` — detection rules and analysis lifecycle
- `docs/METRICS.md` — visibility and confidence metric formulas
- `docs/DATABASE.md` — analysis schema and table definitions

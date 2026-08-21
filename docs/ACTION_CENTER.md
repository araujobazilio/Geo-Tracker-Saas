# Action Center (Phase 9)

## Overview

The Action Center turns persisted Scan evidence into deterministic,
zero-cost action opportunities. `ActionGenerationService` analyzes a
completed STANDARD Scan's immutable evidence and upserts logical
`Opportunity` rows with immutable `OpportunityOccurrence` and typed
`OpportunityEvidence` rows.

It does NOT perform any AI Checks, provider calls, or UsageEvents. It
uses only evidence already persisted from prior scans.

## Key Principles

- **Zero AI Checks** — no LLM calls during action generation.
- **Zero provider calls** — no external HTTP requests.
- **Zero UsageEvents** — no quota consumption.
- **Uses only persisted evidence** — `PromptRun`, `EntityMention`,
  `SourceAttribution`, `ScanEntitySnapshot`.
- **Stable fingerprint** for cross-scan deduplication.
- **Status preservation** across refreshes — human workflow status is
  never overwritten by automated detection.
- **Idempotent** — refreshing the same Scan creates no duplicates.
- **Atomic** — all opportunities/evidence commit together or none.

## Action Engine Version

```
ACTION_ENGINE_VERSION = "deterministic-actions-v1.1"
```

Returned in every `RefreshActionsResponse`. If rules or thresholds
change materially, bump this version.

### v1.1 Changes (Phase 9.1)

- **Concurrent refresh safety**: Project row lock + `IntegrityError`
  handling prevents duplicate Opportunities, Occurrences, or Evidence
  rows when two sessions refresh the same scan (or different scans for
  the same project) simultaneously.
- **Citation eligibility enforcement**: `MIN_CITATION_ELIGIBLE_OBSERVATIONS`
  is now explicitly enforced in `_check_citation_gap`. MODEL_ONLY and
  FAILED runs are excluded from the eligible count.
- **SOV consistency**: Share of Voice now reuses the Phase 7 global
  formula via `VisibilityMetricsService`, ensuring the explanation's SOV
  always matches the metrics endpoint.
- **Prompt-run lineage**: `PROMPT_COMPETITOR_GAP` evidence now includes
  per-`PromptRun` evidence rows (`evidence_type=PROMPT_RUN`) with exact
  SUCCEEDED run IDs. FAILED runs are never included.
- **Occurrence version stamp**: `OpportunityOccurrence.action_engine_version_at_detection`
  records the engine version used when this occurrence was written.
  Historical occurrences from v1 retain `deterministic-actions-v1` via
  `server_default`.

## Rules

### Rule 1: DISCOVERY_VISIBILITY_GAP

A competitor exceeds the brand in NON_BRANDED discovery visibility.

```
MIN_GLOBAL_SUCCESSFUL_OBSERVATIONS = 3
MIN_DISCOVERY_VISIBILITY_GAP_PP   = 10
HIGH_DISCOVERY_VISIBILITY_GAP_PP  = 25
```

- Requires at least `MIN_GLOBAL_SUCCESSFUL_OBSERVATIONS` successful
  observations.
- Gap >= `MIN` (10pp) triggers the opportunity.
- Gap >= `HIGH` (25pp) → priority `HIGH`; otherwise `MEDIUM`.
- Scope: global (no provider filter), `NON_BRANDED`.

### Rule 2: PROVIDER_VISIBILITY_GAP

A competitor exceeds the brand on a single provider in NON_BRANDED
visibility.

```
MIN_PROVIDER_SUCCESSFUL_OBSERVATIONS = 2
MIN_PROVIDER_VISIBILITY_GAP_PP      = 15
HIGH_PROVIDER_VISIBILITY_GAP_PP     = 30
```

- Evaluated per provider in the explanation's provider breakdown.
- Requires at least `MIN_PROVIDER_SUCCESSFUL_OBSERVATIONS` successful
  observations for that provider.
- Gap >= `MIN` (15pp) triggers the opportunity.
- Gap >= `HIGH` (30pp) → priority `HIGH`; otherwise `MEDIUM`.
- One opportunity per (competitor, provider) pair.

### Rule 3: OWNED_CITATION_GAP

A competitor's owned sources are cited more than the brand's owned
sources in citation-eligible observations.

```
MIN_CITATION_ELIGIBLE_OBSERVATIONS = 2
MIN_OWNED_CITATION_GAP_PP          = 20
```

- Requires at least `MIN_CITATION_ELIGIBLE_OBSERVATIONS` citation-eligible
  (WEB_GROUNDED) observations.
- Gap >= `MIN` (20pp) triggers the opportunity.
- Gap >= 40pp → priority `HIGH`; otherwise `MEDIUM`.
- Evidence rows include up to 10 owned source citations.

### Rule 4: PROMPT_COMPETITOR_GAP

A competitor appears in a prompt's response while the brand is absent.

```
MAX_PROMPT_OPPORTUNITIES_PER_COMPETITOR = 5
```

- Capped at `MAX_PROMPT_OPPORTUNITIES_PER_COMPETITOR` prompt gaps per
  competitor.
- Priority is determined by funnel stage, commercial intent, and
  multi-provider presence:

```
PURCHASE + commercial_intent + multi_provider  → HIGH
PURCHASE                                        → MEDIUM
CONSIDERATION + commercial_intent               → MEDIUM
multi_provider (>= 2 affected providers)        → MEDIUM
otherwise                                       → LOW
```

- One opportunity per (competitor, prompt) pair.

## Data Model

### Opportunity

A logical, deduplicated evidence-based gap the user can act on.
Identified by a stable fingerprint (`project_id`, `fingerprint`) so the
same logical issue across multiple Scans updates the same row rather
than creating duplicates. Human workflow status is preserved across
automated refreshes.

Key fields:

| Field | Description |
|-------|-------------|
| `fingerprint` | SHA-256 stable dedup key (64 chars) |
| `opportunity_type` | One of the 4 rule types |
| `status` | Workflow status (OPEN/IN_PROGRESS/IMPLEMENTED/DISMISSED/VERIFIED) |
| `priority` | HIGH/MEDIUM/LOW |
| `action_engine_version` | Version that produced this row |
| `competitor_entity_key` | Stable competitor entity key |
| `provider` | Provider for PROVIDER_VISIBILITY_GAP; NULL otherwise |
| `prompt_id` | Prompt for PROMPT_COMPETITOR_GAP; NULL otherwise |
| `prompt_type` | Always NON_BRANDED in v1 |
| `title` / `summary` / `recommended_action` | Human-readable guidance |
| `first_detected_scan_id` / `latest_detected_scan_id` | Scan lineage |
| `first_detected_at` / `last_detected_at` | Detection timestamps |
| `implemented_at` / `dismissed_at` / `verified_at` | Workflow timestamps |
| `dismissal_reason` | Optional reason when DISMISSED |

Unique constraint: `uq_opportunities_project_fingerprint`
(`project_id`, `fingerprint`).

### OpportunityOccurrence

One immutable record: "This Opportunity was observed in Scan X." A new
Occurrence is created each time a different Scan detects the same
logical Opportunity. Occurrences are never rewritten.

Key fields:

| Field | Description |
|-------|-------------|
| `opportunity_id` | Parent Opportunity |
| `scan_id` | Detecting Scan |
| `scan_analysis_id` | Analysis that produced the evidence |
| `competitor_entity_snapshot_id` | Competitor snapshot at detection |
| `brand_entity_snapshot_id` | Brand snapshot at detection |
| `priority_at_detection` | Priority when this occurrence was written |
| `action_engine_version_at_detection` | Engine version when this occurrence was written |
| `brand_visibility` / `competitor_visibility` | Rates at detection |
| `visibility_gap_pp` | Gap in percentage points |
| `brand_citation_rate` / `competitor_citation_rate` | Citation rates |
| `citation_gap_pp` | Citation gap in percentage points |
| `measurement_coverage` | Coverage at detection |

Unique constraint: `uq_opportunity_occurrences_opp_scan`
(`opportunity_id`, `scan_id`) — enforces idempotency per scan.

### OpportunityEvidence

Typed evidence row backing an Opportunity Occurrence. Each row links to
specific persisted Scan evidence so every Opportunity can be traced back
to immutable measurement data.

Key fields:

| Field | Description |
|-------|-------------|
| `occurrence_id` | Parent Occurrence |
| `evidence_key` | Stable key within the occurrence |
| `evidence_type` | METRIC_GAP / OWNED_SOURCE / PROMPT_RUN |
| `prompt_id` | Linked Prompt (if applicable) |
| `prompt_run_id` | Linked PromptRun (if applicable) |
| `response_source_id` | Linked ResponseSource (if applicable) |
| `provider` | Provider context (if applicable) |
| `metric_name` | Metric identifier (e.g. `visibility_gap_pp`) |
| `brand_value` / `competitor_value` / `delta_value` | Metric values |

Unique constraint: `uq_opportunity_evidence_occ_key`
(`occurrence_id`, `evidence_key`).

## Fingerprint

The fingerprint is a SHA-256 hash of the logical identity of the
opportunity:

```
SHA-256(
  opportunity_type
  | project_id
  | competitor_entity_key
  | provider or "*"
  | prompt_id or "*"
  | prompt_type
)
```

It does NOT include `scan_id`, competitor name, or brand name — those
would break cross-scan deduplication. The same logical gap detected by
different Scans produces the same fingerprint and updates the same
`Opportunity` row.

## Status Workflow

```
OPEN → IN_PROGRESS → IMPLEMENTED
                    ↘ DISMISSED
IN_PROGRESS → OPEN
IMPLEMENTED → IN_PROGRESS → DISMISSED
DISMISSED → OPEN
```

Allowed transitions:

| From | To |
|------|----|
| OPEN | IN_PROGRESS, DISMISSED |
| IN_PROGRESS | OPEN, IMPLEMENTED, DISMISSED |
| IMPLEMENTED | IN_PROGRESS, DISMISSED |
| DISMISSED | OPEN |

Forbidden:

- Any → VERIFIED (Phase 9 cannot verify).
- VERIFIED → anything (read-only).

`VERIFIED` is reserved for Phase 10 Verification Scans.

### Status Preservation Across Refreshes

When a refresh detects an existing Opportunity, it updates mutable
fields (`priority`, `title`, `summary`, `recommended_action`,
`latest_detected_scan_id`, `last_detected_at`) but does NOT overwrite:

- `status`
- `implemented_at`
- `dismissed_at`
- `verified_at`
- `dismissal_reason`

## API

### List Opportunities

```
GET /api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities
```

Query params:

- `status` — filter by OpportunityStatus
- `priority` — filter by OpportunityPriority
- `opportunity_type` — filter by OpportunityType
- `provider` — filter by LLMProvider
- `offset` (>= 0, default 0)
- `limit` (1..100, default 20)

Sorting: status relevance, priority HIGH to LOW, `last_detected_at` DESC.

Response: `OpportunityListResponse`

```json
{
  "items": [
    {
      "id": "uuid",
      "fingerprint": "sha256",
      "opportunity_type": "DISCOVERY_VISIBILITY_GAP",
      "status": "OPEN",
      "priority": "HIGH",
      "action_engine_version": "deterministic-actions-v1.1",
      "title": "...",
      "summary": "...",
      "recommended_action": "...",
      "first_detected_at": "...",
      "last_detected_at": "..."
    }
  ],
  "total": 42,
  "offset": 0,
  "limit": 20
}
```

### Get Opportunity Detail

```
GET /api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}
```

Returns the Opportunity with its latest Occurrence, typed Evidence, and
occurrence count.

Response: `OpportunityDetailResponse` (extends `OpportunityResponse` with
`latest_occurrence`, `occurrence_count`, `reliability_context`).

### Refresh Actions

```
POST /api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}/actions/refresh
```

OWNER/ADMIN only. Performs deterministic local computation only.
Zero AI Checks, zero provider calls.

Response: `RefreshActionsResponse`

```json
{
  "action_engine_version": "deterministic-actions-v1.1",
  "scan_id": "uuid",
  "opportunities_detected": 12,
  "opportunities_created": 5,
  "opportunities_updated": 7,
  "occurrences_created": 9,
  "warnings": []
}
```

### Update Opportunity Status

```
PATCH /api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}
```

OWNER/ADMIN only. Transitions the Opportunity status via
`OpportunityWorkflowService`.

Body:

```json
{
  "status": "IN_PROGRESS",
  "dismissal_reason": null
}
```

Response: `OpportunityResponse` with the updated status.

## Role Matrix

| Role | List / Detail | Refresh | Update Status |
|------|---------------|---------|---------------|
| OWNER | yes | yes | yes |
| ADMIN | yes | yes | yes |
| MEMBER | yes | no | no |

## Tenant Isolation

All endpoints enforce tenant isolation. Cross-tenant access returns 404.

- Workspace A cannot read Workspace B Opportunities.
- Workspace A cannot refresh actions from Workspace B Scans.
- Foreign opportunity/scope mismatch: 404.

## Analysis Readiness

Action generation requires a `COMPLETED` `ScanAnalysis` before producing
any opportunities. A missing, `PENDING`, `RUNNING`, or `FAILED` analysis
is NOT evidence that a gap exists — the service fails closed with:

```
ConflictError("Scan analysis is not completed.")
```

Additional requirements:

- Scan must be `ScanType.STANDARD`.
- Scan must be terminal (`COMPLETED` or `PARTIAL`).
- A single brand snapshot must exist.
- At least one competitor snapshot must exist.

The service does NOT silently populate opportunities when analysis is
unavailable.

## Evidence Lineage

Every `Opportunity` is traceable to immutable Scan evidence:

```
Opportunity
  └─ OpportunityOccurrence (per Scan)
       ├─ scan_id            → Scan
       ├─ scan_analysis_id   → ScanAnalysis
       ├─ competitor_entity_snapshot_id → ScanEntitySnapshot
       ├─ brand_entity_snapshot_id      → ScanEntitySnapshot
       └─ OpportunityEvidence (typed)
            ├─ prompt_run_id      → PromptRun
            ├─ response_source_id → ResponseSource
            └─ prompt_id          → Prompt
```

The full chain resolves back to `EntityMention` and `SourceAttribution`
rows produced by the deterministic Phase 7 analysis engine. No evidence
is fabricated; every metric gap, owned source, and prompt run reference
points at persisted measurement data.

## Safety Cap

```
MAX_OPPORTUNITIES_PER_REFRESH = 50
```

A single refresh never creates more than 50 opportunities. If detection
exceeds the cap, the excess is dropped and a warning is appended to
`RefreshActionsResponse.warnings`:

```
"Detected N opportunities; capping to 50."
```

## See also

- `docs/METRICS.md` — visibility metrics and analysis readiness
- `docs/CONFIDENCE_SCANS.md` — repeat reliability and confidence levels
- `app/core/action_engine.py` — threshold constants
- `app/services/action_generation_service.py` — generation logic
- `app/services/opportunity_workflow_service.py` — status transitions

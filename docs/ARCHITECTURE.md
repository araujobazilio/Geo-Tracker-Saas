# Architecture

## Overview

GEO Tracker is a **multi-tenant web SaaS** built as a **modular monolith**.
The product measures AI visibility (`MEASURE → EXPLAIN → ACT → VERIFY`).

```
Browser
   ↓
FastAPI Application
   ↓
Application / Domain Services
   ↓
PostgreSQL + Redis
   ↓
Celery Workers
   ↓
External AI APIs
```

## Architectural style

**Modular monolith.** Clear boundaries between:

- HTTP layer (`app/routers`)
- domain / application logic (`app/services`)
- persistence (`app/repositories`, `app/db`)
- external providers (`app/providers`)
- billing / entitlements (`app/services/entitlements`, `app/integrations`)
- background jobs (`app/workers`)
- integrations (`app/integrations`)

No microservices are introduced prematurely.

## Multi-tenancy

Tenant boundary = **Workspace**.

```
User → WorkspaceMembership → Workspace → Projects
```

Tenant-access enforcement is implemented: every protected workspace
operation validates membership via `WorkspaceAuthorizationService`.
Cross-tenant access returns 404. Public-facing identifiers are UUIDs
to reduce IDOR risk.

See `docs/MULTITENANCY.md`.

## Service layer

Domain and application logic lives in `app/services`. Each service is a
single responsibility boundary consumed by routers and other services.

| Service | Responsibility |
|---------|----------------|
| `WorkspaceAuthorizationService` | Tenant-access enforcement (membership checks) |
| `AuditService` | Centralized audit logging |
| `EntitlementService` | Resolves effective entitlements for a workspace |
| `QuotaService` | Atomic AI Check quota reservations and usage accounting |
| `ProjectOnboardingService` | Atomic project creation with keywords, competitors, providers, and initial prompt set |
| `ProjectService` | Project CRUD, status transitions (pause/activate/archive), summary |
| `KeywordService` | Keyword CRUD with normalization, capacity enforcement, revision tracking |
| `CompetitorService` | Competitor CRUD with domain normalization, capacity enforcement |
| `ProjectProviderService` | Provider configuration (PUT replace), entitlement enforcement |
| `PromptGenerationService` | Deterministic prompt generation (5 variants/keyword, EN/PT) |
| `PromptSetService` | PromptSet versioning, regeneration, staleness detection |
| `ScanCreationService` | STANDARD preflight, immutable plan creation, full quota reservation, dispatch |
| `ScanExecutionService` | Duplicate-safe row claims and bounded no-retry PromptRun execution; extended for CONFIDENCE round-by-round execution (Phase 8) |
| `PromptRunResultRecorder` | Atomic evidence, source, cost, UsageEvent, and one-check commit |
| `ScanFinalizationService` / `ScanRecoveryService` | Atomic terminal classification + unused quota release (single commit), idempotent reconciliation, run-count invariant, stale PENDING/RUNNING recovery without provider replay |
| `ConfidenceScanCreationService` | Clones a baseline STANDARD scan's methodology to create a CONFIDENCE scan with repeated observations (Phase 8) |
| `ConfidenceMetricsService` | Computes reliability metrics (measurement coverage, mention stability, confidence level) from repeated CONFIDENCE observations (Phase 8) |
| `CompetitorExplanationService` | Computes evidence-based brand vs competitor explanations from persisted Scan analysis evidence (Phase 9); extended for VERIFICATION scans in Phase 10 |
| `ActionGenerationService` | Deterministic opportunity generation from completed STANDARD scan evidence; fingerprint-based cross-scan dedup; status-preserving upsert (Phase 9) |
| `VerificationScanCreationService` | Clones a frozen implementation baseline STANDARD scan's methodology to create a single-repeat VERIFICATION scan with targeted scope (Phase 10 + 10.1) |
| `VerificationEvaluationService` | Deterministic before/after comparison producing VerificationOutcome; zero AI Checks; system-only VERIFIED transition on RESOLVED; automatic evaluation after analysis (Phase 10 + 10.1) |
| `OpportunityWorkflowService` | Opportunity status transitions with audit timestamps; freezes implementation baseline on IMPLEMENTED, clears on IN_PROGRESS, system-only VERIFIED transition on RESOLVED outcome (Phase 9 + Phase 10 + 10.1) |
| `PricingService` / `ProviderCostCalculator` | Exact effective price resolution and Decimal/null-safe cost calculation |

### Entitlement resolution

`EntitlementService` resolves what a workspace is entitled to via a
single chain:

```
BillingAccount (primary, eligible status) → plan_code → PlanDefinition → EffectiveEntitlements
```

It is **fail-safe**: if there is no primary billing account, the status
is not eligible, the plan code is missing/unknown, or the plan is
inactive, it returns a conservative `UNENTITLED` snapshot (all limits
zero, all flags false, no providers). It never raises. Routers and
services consume the immutable `EffectiveEntitlements` value object,
never billing tables directly.

See `docs/ENTITLEMENTS.md`.

### Quota reservation flow

`QuotaService` enforces AI Check quotas atomically using PostgreSQL
row-level locking (`SELECT ... FOR UPDATE`):

```
reserve_ai_checks()  →  ACTIVE reservation (quota held)
        ↓
provider call executes
        ↓
commit_ai_checks()   →  COMMITTED (used incremented, UsageEvent created)
        or
release_reservation() → RELEASED (unused reserved returned)
        or
expire_stale_reservations() → EXPIRED (TTL passed)
```

PostgreSQL is the quota source of truth (NOT Redis). The monthly quota
period is the UTC calendar month. Reservations are idempotent via
`idempotency_key` and retained after completion for history.

See `docs/USAGE_AND_QUOTAS.md`.

## Provider abstraction (Phase 5/5.1)

AI provider adapters live in `app/providers`. They translate
`ProviderRequest` into provider-specific HTTP calls and normalize
responses into `ProviderResult`.

```
ProviderRequest → ProviderAdapter.execute() → ProviderResult
```

Key design decisions:

- **Provider vs surface**: `LLMProvider` identifies the company;
  `ProviderSurface` identifies the specific API endpoint. API results
  measure the surface, not the consumer UI.
- **Execution modes**: `MODEL_ONLY` (no web search) and `WEB_GROUNDED`
  (web search tool). Not all providers support all modes.
- **WEB_GROUNDED integrity**: WEB_GROUNDED success ALWAYS implies
  `search_used == True`. If no search is observed, `ProviderSearchError`
  is raised. This is a critical methodological invariant.
- **No automatic retries**: One `execute()` = at most ONE billable
  request. The Phase 6 Scan Engine, Celery task, and stale recovery also never
  replay provider calls.
- **No quota/usage in adapters**: Adapters do NOT call `QuotaService`
  or create `UsageEvent`. Scan Engine owns accounting.
- **No hidden system prompts**: The prompt text is the experimental
  input. Adapters add only the minimum API envelope.
- **Request ID vs response ID**: `provider_request_id` is the HTTP
  request/support identifier (e.g. `x-request-id` header);
  `provider_response_id` is the generated resource/object identifier
  (e.g. response ID, message ID, interaction ID). These are distinct.
- **Provider-reported cost**: `provider_reported_cost_usd` (Decimal)
  preserves the cost reported by the provider (e.g. Perplexity), not
  our own pricing calculation.
- **Malformed JSON normalization**: All adapters normalize invalid JSON
  to `ProviderResponseError`. Parser exceptions never leak.
- **httpx.AsyncClient**: Unified transport for all providers;
  `MockTransport` for deterministic tests.
- **Lazy registry**: `ProviderRegistry` constructs adapters on demand.
  Missing credentials do NOT crash application startup.
  `capabilities()` works WITHOUT credentials (static adapter facts).
- **Google Interactions API**: The Google adapter uses the current
  stable Interactions API (`POST /v1/interactions`), not the legacy
  `generateContent`. `store=false` for stateless one-shot
  measurements. Thought/reasoning steps are discarded.

See `docs/PROVIDER_INTEGRATIONS.md` and `docs/PROVIDER_COMPLIANCE.md`.

## Scan Engine (Phase 6)

A STANDARD scan snapshots one exact active PromptSet and creates one PromptRun
for every active-prompt × eligible-provider pair before any provider request.
The fixed policy is OpenAI/Anthropic/Perplexity `WEB_GROUNDED` and Google
`MODEL_ONLY`; project, entitlement, model, or PromptSet changes after creation
do not mutate the stored plan.

The complete `planned_ai_checks` amount is reserved before dispatch. Celery
carries only the Scan UUID; PostgreSQL row claims guard duplicate deliveries.
Workers use early acknowledgement, bounded async concurrency with a separate
session per run, and no autoretry or `self.retry`. A successful result atomically
stores PromptRun evidence, ordered ResponseSource rows, cost, one UsageEvent,
and exactly one customer AI Check. Provider-internal searches may increase
provider cost but do not increase customer checks. Failed runs consume zero;
valid answers without a brand mention are successful measurements. Phase 7 must
exclude failed runs from metric denominators.

Stale recovery marks unresolved work failed and releases unused reservation; it
never repeats provider requests, absorbing worker-loss ambiguity as GEO cost.
Customer Scan APIs expose evidence but deliberately hide internal cost fields.
See `docs/SCAN_ENGINE.md` and `docs/COST_ACCOUNTING.md`.

## Phase 7: Deterministic visibility analysis

Phase 7 introduces a deterministic, AI-free analysis pipeline that runs
after scan finalization. It produces persisted evidence (mentions and
source attributions) and computes visibility metrics from that evidence.
Zero AI Checks, zero provider calls.

```
Scan finalized
   ↓
ScanFinalizationService triggers analysis (separate session)
   ↓
ScanAnalysisService loads immutable ScanEntitySnapshots
   ↓
For each SUCCEEDED PromptRun:
   mention_detector  →  EntityMention records
   source_attributor →  SourceAttribution records (OWNED_DOMAIN only)
   ↓
Evidence persisted atomically (row-locked, idempotent)
   ↓
VisibilityMetricsService computes metrics from persisted evidence
```

### New components

| Component | Responsibility |
|-----------|----------------|
| `mention_detector.py` | Deterministic text matching for brand/competitor mentions. Case-insensitive, token-boundary-aware (no substring false positives). Produces `EntityMention` records. |
| `source_attributor.py` | URL host parsing and domain matching to attribute `ResponseSource` URLs to tracked entities. Produces `SourceAttribution` records. Phase 7 supports `OWNED_DOMAIN` attribution only. |
| `scan_analysis_service.py` | Orchestrates analysis: loads immutable `ScanEntitySnapshot`s, runs mention detection + source attribution on each SUCCEEDED `PromptRun`, persists evidence atomically. Idempotent (re-running returns existing COMPLETED analysis). Concurrent-safe via row locking. Zero AI Checks, zero provider calls. |
| `visibility_metrics_service.py` | Computes all visibility metrics from persisted evidence: visibility rate, mention counts, citation counts, share of voice, measurement coverage, provider breakdown, leaderboard. Distinguishes zero vs null semantics for `visibility_rate`. Requires COMPLETED analysis (Phase 8.1). |
| `analysis.py` (router) | Analysis API endpoints for running/getting analysis and metrics. Tenant-isolated, role-enforced (ADMIN for POST, MEMBER for GET). |
| `analysis_repository.py` | Data access for `ScanAnalysis`, `EntityMention`, `SourceAttribution`, `ScanEntitySnapshot`. |

### New models

`app/models/analysis.py` introduces:

- **`ScanEntitySnapshot`** — immutable copy of brand + competitors at
  scan creation time.
- **`ScanAnalysis`** — analysis run state for a scan (PENDING →
  COMPLETED/FAILED).
- **`EntityMention`** — a detected brand/competitor mention within a
  `PromptRun` response.
- **`SourceAttribution`** — a `ResponseSource` URL attributed to a
  tracked entity.

### Service extensions

- **`ScanCreationService`** — now creates immutable
  `ScanEntitySnapshot` rows during scan creation, copying brand +
  competitors at that point in time.
- **`ScanFinalizationService`** — auto-triggers analysis after
  finalization in a separate session. Analysis failure does not affect
  scan state.

## Phase 8: Confidence Scans

Phase 8 introduces `CONFIDENCE` scans that repeat the same Prompt × Provider
cells `repeat_count` times to measure response reliability. A CONFIDENCE scan
is always derived from a terminal `STANDARD` scan (the baseline) and clones its
methodology.

```
Baseline STANDARD scan (terminal)
   ↓
ConfidenceScanCreationService clones methodology + creates repeated PromptRuns
   ↓
ScanExecutionService executes round-by-round (observation_index 1..N)
   ↓
ScanFinalizationService classifies terminal state + releases unused quota
   ↓
ConfidenceMetricsService computes reliability metrics from repeated observations
```

### New components

| Component | Responsibility |
|-----------|----------------|
| `confidence_scan_creation_service.py` | Clones the baseline STANDARD scan's snapshotted prompt set, provider targets, execution modes, and model IDs. Creates `prompt_count × provider_count × repeat_count` `PromptRun` rows with appropriate `observation_index` values. Validates baseline scan is terminal and same-workspace. |
| `confidence_metrics_service.py` | Computes reliability metrics from repeated CONFIDENCE observations: measurement coverage, repeat sufficiency, mention stability, round visibility, observed visibility range, and `MeasurementConfidenceLevel` (INSUFFICIENT/LOW/MEDIUM/HIGH). Requires COMPLETED analysis. Provider breakdown is fully provider-scoped (Phase 8.1). |
| `confidence.py` (router) | Confidence scan API endpoints: create CONFIDENCE scan from baseline, retrieve confidence results and metrics. Tenant-isolated, role-enforced. |

### Service extensions

- **`ScanExecutionService`** — extended for round-by-round execution of
  `CONFIDENCE` scans. Groups `PromptRun` rows by `observation_index`,
  executes each round fully before advancing to the next, and reuses the
  same atomic result recording, finalization, and stale-recovery machinery
  as STANDARD scans.

## Phase 9: Action Center and Competitor Explanations

Phase 9 introduces evidence-based competitor explanations and a
deterministic Action Center. Both features are **zero-cost**: they
consume no AI Checks, no UsageEvents, and no paid provider calls. They
operate exclusively on persisted Scan evidence.

```
COMPLETED STANDARD Scan + Analysis
   ↓
CompetitorExplanationService computes brand vs competitor gaps
   ↓
ActionGenerationService upserts Opportunities (fingerprint-deduped)
   ↓
User reviews, transitions status (OPEN → IN_PROGRESS → IMPLEMENTED)
```

### New components

| Component | Responsibility |
|-----------|----------------|
| `competitor_explanation_service.py` | Computes deterministic brand vs competitor explanations from persisted evidence: visibility rates, overlap matrix, provider breakdown, owned citation rates, prompt gaps, optional reliability context. Requires COMPLETED analysis. Uses historical `ScanEntitySnapshot` (not current Project state). |
| `action_generation_service.py` | Generates deterministic Opportunities from completed STANDARD scan evidence using 4 rules. Fingerprint-based cross-scan dedup. Status-preserving upsert. Idempotent per scan. Atomic per refresh. |
| `opportunity_workflow_service.py` | Manages Opportunity status transitions with audit timestamps. VERIFIED is read-only (reserved for Phase 10). |
| `action_engine.py` | Constants for all action generation thresholds and version identifier. |
| `opportunities.py` (router) | Action Center API endpoints: list/get opportunities, refresh actions, update status. Tenant-isolated, role-enforced. |
| `competitor_explanation.py` (router) | Competitor explanation API endpoints: list summaries, get detail. Tenant-isolated, role-enforced. |

### New models

- **`Opportunity`** — logical, deduplicated workflow entity identified by
  a stable SHA-256 fingerprint. Human workflow status is preserved across
  automated refreshes.
- **`OpportunityOccurrence`** — immutable per-scan record. A new
  Occurrence is created each time a different Scan detects the same
  logical Opportunity.
- **`OpportunityEvidence`** — typed evidence row linking to specific
  persisted Scan evidence (PromptRun, ResponseSource, metric gap).

### Key principles

- **Zero-cost**: No AI Checks, no provider calls, no UsageEvents.
- **Evidence-based**: Uses only persisted evidence
  (`PromptRun`, `EntityMention`, `SourceAttribution`,
  `ScanEntitySnapshot`).
- **No causal claims**: Explanations describe observed patterns, not
  causation.
- **Fail closed**: Requires COMPLETED analysis. Missing or FAILED
  analysis raises `ConflictError`, not zero visibility.
- **Historical snapshot integrity**: Uses `ScanEntitySnapshot`
  names/domains, not current mutable `Project`/`Competitor` state.
- **Idempotent**: Refreshing the same Scan creates no duplicates.
- **Status preservation**: `IN_PROGRESS`/`IMPLEMENTED`/`DISMISSED`
  Opportunities are not reopened by new scan refreshes.

See `docs/ACTION_CENTER.md` and `docs/COMPETITOR_EXPLANATIONS.md`.

### Phase 9.1: Correctness Pass

- **Concurrent refresh safety**: Project row lock + `IntegrityError`
  handling prevents duplicate rows when multiple sessions refresh
  simultaneously.
- **Citation eligibility**: `MIN_CITATION_ELIGIBLE_OBSERVATIONS` enforced
  in `_check_citation_gap`. `citation_eligible_observations` exposed on
  `CompetitorExplanation` and `ProviderExplanation`.
- **SOV consistency**: `CompetitorExplanationService` reuses
  `VisibilityMetricsService` for Share of Voice, guaranteeing
  explanation/metrics parity.
- **Prompt-run lineage**: `PROMPT_COMPETITOR_GAP` evidence includes
  per-`PromptRun` rows with exact SUCCEEDED run IDs.
- **Action engine v1.1**: `ACTION_ENGINE_VERSION` bumped to
  `deterministic-actions-v1.1`. `OpportunityOccurrence.action_engine_version_at_detection`
  records the engine version at detection time.

## Phase 10 + 10.1: Verification Scans and Opportunity Outcome Tracking

Phase 10 introduces **Verification Scans** and **Opportunity Outcome
Tracking** — a deterministic, zero-AI-Check before/after comparison
that measures whether a user's implementation work resolved a
previously detected Action Center Opportunity. Phase 10.1 hardens
the verification scope, outcome integrity, and automatic evaluation.

### Key concepts

- **Implementation baseline freezing**: When an Opportunity transitions
  to `IMPLEMENTED`, the latest eligible `OpportunityOccurrence` is
  frozen as `implementation_baseline_occurrence_id`. Returning to
  `IN_PROGRESS` clears it.
- **Methodology cloning**: `VerificationScanCreationService` clones the
  frozen baseline STANDARD scan's exact prompts, providers, surfaces,
  execution modes, requested models, and entity snapshots. Current
  project configuration cannot alter the cloned methodology.
- **Deterministic evaluation**: `VerificationEvaluationService`
  computes the verification metrics via `CompetitorExplanationService`,
  compares to the frozen baseline, and persists a
  `VerificationOutcome`. Zero AI Checks, zero provider calls.
- **VERIFIED status**: Only a `RESOLVED` outcome transitions the
  Opportunity to `VERIFIED` via the system-only
  `mark_verified_from_verification()` method. `VERIFIED` is read-only.
- **No causal claims**: `VERIFIED` does NOT prove the user's
  implementation caused the improvement. It means the originally
  measured issue is no longer present under the verification
  methodology.

### New files

| File | Purpose |
|------|---------|
| `app/core/verification_engine.py` | Constants for verification thresholds and methodology version |
| `app/core/verification_scope.py` | `VerificationScope`, `VerificationTargetCell`, `VerificationScopeResolver` — targeted scope per OpportunityType (Phase 10.1) |
| `app/models/opportunity.py` (extended) | `OpportunityVerification` model, `implementation_baseline_occurrence_id` on `Opportunity` |
| `app/services/verification_scan_creation_service.py` | Clones baseline methodology with targeted scope, creates VERIFICATION scan + OpportunityVerification record |
| `app/services/verification_evaluation_service.py` | Deterministic before/after comparison, persists outcome, system-only VERIFIED transition, automatic evaluation |
| `app/schemas/verification.py` | Pydantic schemas for verification API |
| `app/routers/api/verification.py` | Verification API endpoints (create, list, detail, evaluate, summary) |

### Service extensions

- **`OpportunityWorkflowService`** — `transition()` now freezes the
  baseline occurrence on IMPLEMENTED and clears it on IN_PROGRESS.
  `mark_verified_from_verification()` provides the system-only path to
  VERIFIED with independent validation. Direct manual transitions to
  VERIFIED are forbidden.
- **`CompetitorExplanationService`** — `_require_standard_scan()`
  extended to accept VERIFICATION scans (same methodology). Phase 10.1
  adds `prompt_id` and `execution_mode` filters for scoped evaluation.
- **`ScanFinalizationService`** — Phase 10.1: automatically triggers
  `VerificationEvaluationService.evaluate()` after a VERIFICATION
  scan's analysis completes.

### Phase 10.1 hardening

- **Targeted scope**: `VerificationScopeResolver` selects exact
  historical baseline cells per OpportunityType (not the full
  Cartesian product). `planned_ai_checks = len(target_cells)`.
- **Brand-side safeguards**: gap closing to 0 because the brand
  disappeared is NOT a resolution.
- **Baseline coverage gate**: baseline coverage < 75% → INCONCLUSIVE.
- **Two-sided citation sufficiency**: both baseline AND verification
  must have sufficient citation-eligible observations.
- **PROMPT_COMPETITOR_GAP**: uses `competitor_only_rate` (pp) not
  `competitor_only_count` (integer).
- **Automatic evaluation**: triggered after ScanAnalysis completes.
- **One pending verification per cycle**: enforced at service + DB
  level (partial unique index).
- **Idempotency hardening**: same key + different baseline =
  ConflictError.

### Phase 10.3 hardening

- **Auto-terminalization**: `ScanFinalizationService` now guarantees
  that a `PENDING` `OpportunityVerification` is never left stranded
  after its scan reaches a terminal state. A centralized
  `_post_finalize_verification_lifecycle` helper handles all paths.
- **FAILED scan → INCONCLUSIVE**: when a VERIFICATION scan is FAILED
  (all runs failed or quota exceeded), the verification is
  automatically terminalized as `INCONCLUSIVE` /
  `VERIFICATION_SCAN_FAILED`.
- **Analysis exception → INCONCLUSIVE**: when
  `ScanAnalysisService.analyze()` raises after persisting
  `ScanAnalysis=FAILED`, the verification is terminalized as
  `INCONCLUSIVE` / `ANALYSIS_NOT_COMPLETED`.
- **Evaluation exception → PENDING**: ephemeral evaluation errors
  leave the verification `PENDING` for manual retry (no provider
  replay).
- **Idempotent self-healing**: re-calling `finalize()` on an
  already-terminal FAILED scan reconciles any stranded PENDING
  verification.

See `docs/VERIFICATION_SCANS.md` for full details.

## Technology stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI |
| ORM | SQLAlchemy 2.x |
| Validation | Pydantic v2 |
| Database | PostgreSQL 15+ |
| Migrations | Alembic |
| Cache / broker | Redis |
| Background jobs | Celery + Celery Beat |
| Frontend | Jinja2, HTMX, Tailwind CSS, Chart.js (planned) |
| Auth | Opaque server-side sessions, HttpOnly cookies, Argon2id |
| Infra | Docker, Docker Compose, Nginx, HTTPS (production) |
| Quality | pytest, Ruff, mypy (strict) |

## Configuration

All configuration is environment-driven via `pydantic-settings`
(see `app/config.py` and `.env.example`). No secrets are hardcoded.

## Logging

Structured logging via `structlog`. JSON output in production,
human-readable in development. Logs never contain secrets.

## Health / readiness

- `GET /health` — liveness, no external dependencies.
- `GET /ready` — readiness, verifies PostgreSQL + Redis.

These endpoints live outside `/api/v1` versioning.

## API versioning

Application APIs are namespaced under `/api/v1/` (added in later phases).
Infrastructure endpoints (`/health`, `/ready`) are unversioned.

## Roadmap status

| Feature | Status |
|---------|--------|
| Application foundation | IMPLEMENTED (Phase 0) |
| Core multi-tenant data model | IMPLEMENTED (Phase 1) |
| Authentication, workspaces, authorization | IMPLEMENTED (Phase 2) |
| Entitlements / quotas | IMPLEMENTED (Phase 3) |
| Project onboarding / prompts | IMPLEMENTED (Phase 4) |
| AI provider abstraction | IMPLEMENTED (Phase 5) |
| Scan Engine | IMPLEMENTED (Phase 6) |
| Brand / citation detection | IMPLEMENTED (Phase 7) |
| Confidence Scans | IMPLEMENTED (Phase 8) |
| Action Center | IMPLEMENTED (Phase 9) |
| Verification system | IMPLEMENTED (Phase 10 + 10.1) |
| Scheduling / email reports | PLANNED (Phase 11) |
| Dashboard / UI | PLANNED (Phase 12) |
| Agency dashboard / white-label | PLANNED (Phase 13) |
| AppSumo licensing | PLANNED (Phase 14) |
| Stripe billing | PLANNED (Phase 15) |
| Admin / observability | PLANNED (Phase 16) |
| Security hardening | PLANNED (Phase 17) |
| AppSumo launch prep | PLANNED (Phase 18) |

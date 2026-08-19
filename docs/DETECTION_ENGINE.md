# Detection Engine

## Status

**IMPLEMENTED (Phase 7).** Deterministic brand/competitor detection,
citation attribution, and visibility metrics are computed from persisted
PromptRun evidence without any AI analysis, provider API calls, or AI
Check consumption.

## Overview

The detection engine analyzes SUCCEEDED PromptRun response text and
ResponseSource URLs against immutable ScanEntitySnapshots. It produces
two types of derived evidence:

- **EntityMentions**: occurrences of tracked entity terms in response text
- **SourceAttributions**: ResponseSource URLs attributed to entities via
  domain matching

All detection is **deterministic** — no LLM, no embeddings, no external
calls. The same input always produces the same output.

## Mention detection

### Matching rules

- **Case-insensitive** Unicode-aware matching (NFKC normalization).
- **Token/phrase boundary semantics**: no substring false positives
  (e.g., "Acme" does NOT match "Acmeology"; "Notion" does NOT match
  "Notional").
- **Multi-word terms** match natural whitespace variants (e.g., "Acme
  CRM" matches "Acme   CRM" with extra spaces).
- **Overlapping terms** for the same entity deduplicate to the longest
  match. Genuinely distinct occurrences are preserved.
- **Different entities** can have overlapping text spans (both are
  recorded).
- **Domains** are matched as standalone tokens (e.g., "acme.com" in
  text), optionally with `www.` prefix or URL scheme.

### Term types

| Type | Source | Example |
|------|--------|---------|
| `NAME` | Entity name | "Acme" |
| `ALIAS` | Entity aliases | "Acme CRM" |
| `DOMAIN` | Entity domain | "acme.com" |

### Ambiguity handling

When the same normalized NAME/ALIAS term belongs to multiple entities,
that term is **excluded from all entities** and a warning is recorded.
Domains remain separately attributable even if shared (e.g., two
entities tracking the same domain both get attribution).

### Occurrence indexing

Each mention is assigned an `occurrence_index` per (PromptRun, entity)
pair, following the original response order (1-based). The `start_index`
and `end_index` columns record the character offsets in the response
text. The `matched_term` column stores the normalized term that was
matched, while `matched_text` stores the actual substring from the
response.

## Source attribution

### Domain matching rules

- **Exact domain match**: `host == domain` (e.g., `acme.com` matches
  `acme.com`).
- **Subdomain match**: `host` ends with `"." + domain` (e.g.,
  `blog.acme.com` matches `acme.com`).
- **Most-specific domain wins**: when multiple tracked domains match,
  the one with the most labels wins (e.g., `product.acme.com` wins over
  `acme.com` for a `blog.product.acme.com` source).
- **No naive substring/suffix matching**: `evilacme.com` does NOT match
  `acme.com`; `notacme.com` does NOT match `acme.com`.

### URL parsing

- Uses safe `urllib.parse` only — no DNS, no HTTP, no redirect
  resolution.
- Does NOT modify the stored `ResponseSource.url`.
- Invalid URLs are skipped with a warning, not a failure.
- Hostnames are validated (must contain a dot and only valid hostname
  characters).

### Attribution type

Phase 7 only produces `OWNED_DOMAIN` attributions: the source hostname
matches the entity's tracked domain. Source title/entity semantic
analysis is NOT performed — only hostname attribution counts as "owned
citation".

## Analysis lifecycle

```
Scan finalized (COMPLETED/PARTIAL)
        ↓
Auto-trigger ScanAnalysisService.analyze()
        ↓
Load ScanEntitySnapshots (immutable)
        ↓
Build entity terms + domains
        ↓
For each SUCCEEDED PromptRun:
  - Detect mentions in response_text
  - Attribute ResponseSource URLs
        ↓
Persist EntityMention + SourceAttribution rows atomically
        ↓
ScanAnalysis.status = COMPLETED
```

### Idempotency

- Re-analyzing the same `(scan_id, analysis_version)` returns the
  existing COMPLETED analysis without duplicating evidence. The
  `analysis_version` is a string (currently `"deterministic-entity-v1"`)
  that identifies the algorithm version.
- A FAILED analysis can be safely retried (old evidence is deleted
  before re-inserting).
- Concurrent analysis is prevented via row locking + unique constraints.

### Zero-cost guarantee

Analysis consumes **0 AI Checks**, makes **0 provider calls**, and
creates **0 UsageEvents**. It operates entirely on persisted evidence.

### Failure isolation

Analysis failure MUST NOT rollback scan completion, change quota, or
repeat providers. The scan remains terminal regardless of analysis
outcome. Analysis runs in a separate session from finalization.

## Eligibility

Analysis is applicable only to:
- Terminal scans (`COMPLETED` or `PARTIAL` status)
- Scans with at least one SUCCEEDED PromptRun

Non-terminal scans and all-failed scans are rejected with a
`ValidationError`.

Scans without entity snapshots fail with `MISSING_ENTITY_SNAPSHOT`
(this can happen for scans created before Phase 7).

## API endpoints

| Method | Path | Role | Description |
|--------|------|------|-------------|
| POST | `/api/v1/workspaces/{wid}/projects/{pid}/scans/{sid}/analysis` | ADMIN | Run or retry deterministic analysis |
| GET | `/api/v1/workspaces/{wid}/projects/{pid}/scans/{sid}/analysis` | MEMBER | Get current analysis |
| GET | `/api/v1/workspaces/{wid}/projects/{pid}/scans/{sid}/metrics` | MEMBER | Get visibility metrics |
| GET | `/api/v1/workspaces/{wid}/projects/{pid}/scans/{sid}/runs/{rid}/analysis` | MEMBER | Get per-run evidence |

All endpoints enforce tenant isolation. Cross-tenant access returns 404.

See `docs/METRICS.md` for metric formulas and `docs/DATABASE.md` for
the analysis schema.

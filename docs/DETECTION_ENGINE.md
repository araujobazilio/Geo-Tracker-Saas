# Detection Engine

## Status

**IMPLEMENTED (Phase 7 + Phase 7.1 hardening).** Deterministic
brand/competitor detection, citation attribution, and visibility
metrics are computed from persisted PromptRun evidence without any AI
analysis, provider API calls, or AI Check consumption.

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
- **Domains** use hostname-aware boundary matching (see below).

### Domain boundary matching (Phase 7.1)

Domain mentions in text use **hostname-aware left and right boundaries**
to prevent false positives:

- **Left boundary**: the domain must NOT be preceded by an alphanumeric
  or hyphen character. This prevents `notacme.com` and `fakeacme.com`
  from matching `acme.com`. A dot IS allowed before the domain, so
  subdomain text like `blog.acme.com` correctly matches `acme.com`.
- **Right boundary**: the domain must NOT be followed by a dot +
  alphanumeric label. This prevents `acme.com.attacker.test` from
  matching `acme.com`. Trailing punctuation like `acme.com.` or
  `acme.com,` still matches because the punctuation is not followed by
  a label.

The `matched_text` captures only the tracked domain portion (e.g.,
`acme.com`), not any subdomain prefix that may precede it in the text.

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

### Unicode normalization

Configured terms are NFKC-normalized and lowercased before matching.
The regex engine matches against the **original** response text (not a
normalized copy), so `start_index`/`end_index` always point to the exact
substring in the original response. If the response uses a canonically
equivalent but different Unicode form (e.g., decomposed NFD vs
composed NFC), the regex may not match because the raw byte sequences
differ. This is a known limitation.

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

### Ambiguous source domains (Phase 7.1)

When a source host matches multiple tracked domains with **equal
specificity** (e.g., two entities tracking the same domain), the
attribution is **NOT assigned** and an `AMBIGUOUS_SOURCE_DOMAIN` warning
is recorded. This prevents incorrect attribution to the wrong entity.

The `attribute_source()` function returns a typed
`SourceAttributionDecision` with one of three outcomes:

| Outcome | Meaning |
|---------|---------|
| `NO_MATCH` | No tracked domain matched the source host |
| `ATTRIBUTED` | Exactly one entity matched (most-specific wins) |
| `AMBIGUOUS` | Multiple equally-specific domains matched; no attribution |

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
ScanExecutionService.execute_scan()
        ↓
Finalize scan (COMPLETED/PARTIAL)
        ↓
Auto-trigger ScanAnalysisService.analyze() [trigger_analysis=True]
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

### Automatic triggering (Phase 7.1)

`ScanExecutionService.execute_scan()` automatically calls
`ScanFinalizationService.finalize(trigger_analysis=True)` after all runs
complete. The analysis runs in a fresh session with the same session
factory used by the execution service. No manual API call is needed to
produce analysis evidence.

Direct calls to `ScanFinalizationService.finalize()` default to
`trigger_analysis=True` but accept `trigger_analysis=False` to skip
analysis (used in finalization-only tests and recovery flows).

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

### Failure isolation and persistence (Phase 7.1)

Analysis failure MUST NOT rollback scan completion, change quota, or
repeat providers. The scan remains terminal regardless of analysis
outcome. Analysis runs in a separate session from finalization.

**Unexpected exceptions** (e.g., `INTERNAL_ERROR`) are persisted as a
FAILED `ScanAnalysis` record in a **separate transaction** from the
main analysis. This ensures the failure is always auditable even when
the main analysis transaction rolls back. The failure session factory
is injected via `failure_session_factory` and uses these rules:

- If a COMPLETED analysis already exists, it is NOT downgraded.
- If a RUNNING/PENDING row exists (owned by the failed attempt), it
  transitions to FAILED.
- If no row exists (creation was rolled back), a fresh FAILED record is
  created.
- `IntegrityError` races (concurrent worker created the row) are handled
  by re-reading and only transitioning non-terminal rows.

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

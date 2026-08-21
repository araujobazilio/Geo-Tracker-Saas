# Visibility Metrics

## Status

**IMPLEMENTED (Phase 7).** All metrics are computed from persisted
detection evidence (EntityMentions, SourceAttributions) without any AI
analysis or provider calls.

## Overview

The `VisibilityMetricsService` computes visibility metrics from the
deterministic analysis evidence. Metrics are grouped into:

1. **Entity metrics** — per-entity visibility, mention counts, citation
   counts
2. **Provider breakdown** — per-provider visibility for the brand
3. **Aggregate metrics** — share-of-voice, measurement coverage

## Metric definitions

### Visibility rate

```
visibility_rate = measured_mentions / measured_prompts
```

- `measured_prompts`: SUCCEEDED PromptRuns where the entity was
  "measured" (i.e., the entity's terms were searched for in the
  response text).
- `measured_mentions`: SUCCEEDED PromptRuns where the entity was
  actually mentioned.
- **Range**: [0.0, 1.0]
- **Null semantics**: `measured_prompts == 0` → `visibility_rate is
  None` (entity was not measured in any prompt).
- **Zero semantics**: `measured_prompts > 0` and `measured_mentions ==
  0` → `visibility_rate == 0.0` (entity was measured but never
  mentioned).

### Mention counts

| Field | Description |
|-------|-------------|
| `mention_count` | Total occurrences across all measured prompts |
| `measured_mentions` | Prompts where the entity was mentioned (≥1 occurrence) |
| `measured_prompts` | Prompts where the entity's terms were searched |

### Citation counts

| Field | Description |
|-------|-------------|
| `citation_count` | Total ResponseSource URLs attributed to this entity |
| `prompts_with_citation` | Prompts with ≥1 attributed citation |

### Share of voice

```
share_of_voice = entity_mention_count / total_mention_count
```

- Computed across all entities (brand + competitors).
- `total_mention_count == 0` → `share_of_voice is None` for all
  entities.
- **Range**: [0.0, 1.0]
- **Phase 9.1**: `CompetitorExplanationService` reuses this formula via
  `VisibilityMetricsService` to guarantee SOV consistency between the
  metrics and explanation endpoints.

### Measurement coverage

```
measurement_coverage = measured_prompts / total_succeeded_prompts
```

- `total_succeeded_prompts`: all SUCCEEDED PromptRuns for the scan.
- **Range**: [0.0, 1.0]
- Indicates how many prompts actually measured the entity.

### Provider breakdown

For each provider that ran prompts for the scan:

| Field | Description |
|-------|-------------|
| `provider` | LLM provider name |
| `total_prompts` | SUCCEEDED prompts for this provider |
| `brand_mentioned_prompts` | Prompts where the brand was mentioned |
| `brand_visibility_rate` | `brand_mentioned_prompts / total_prompts` |
| `brand_citation_count` | Citations attributed to the brand |

### Leaderboard

Entities are sorted by `visibility_rate` descending, then by
`mention_count` descending, then by name ascending. Entities with
`visibility_rate is None` sort last.

## Zero vs null semantics

| Scenario | `visibility_rate` | `measured_prompts` | `measured_mentions` |
|----------|-------------------|--------------------|---------------------|
| Entity mentioned in 3/5 prompts | `0.6` | `5` | `3` |
| Entity not mentioned in any prompt | `0.0` | `5` | `0` |
| Entity not measured in any prompt | `None` | `0` | `0` |

This distinction is critical:
- `0.0` means the entity was measured but never mentioned (bad
  visibility).
- `None` means the entity was not measured (data gap, not a visibility
  signal).

## API response

```json
{
  "scan_id": "uuid",
  "analysis_id": "uuid",
  "analysis_version": "deterministic-entity-v1",
  "analysis_status": "COMPLETED",
  "total_succeeded_prompts": 10,
  "total_mention_count": 15,
  "entity_metrics": [
    {
      "entity_snapshot_id": "uuid",
      "entity_type": "BRAND",
      "name": "Acme",
      "domain": "acme.test",
      "ordinal": 1,
      "mention_count": 8,
      "measured_mentions": 5,
      "measured_prompts": 10,
      "visibility_rate": 0.5,
      "citation_count": 3,
      "prompts_with_citation": 2,
      "share_of_voice": 0.533,
      "measurement_coverage": 1.0
    }
  ],
  "provider_breakdown": [
    {
      "provider": "OPENAI",
      "total_prompts": 5,
      "brand_mentioned_prompts": 3,
      "brand_visibility_rate": 0.6,
      "brand_citation_count": 2
    }
  ]
}
```

## Determinism

All metrics are computed from persisted evidence. The same analysis
evidence always produces the same metrics. No randomness, no AI, no
external calls.

## Confidence / Reliability Metrics (Phase 8)

**IMPLEMENTED (Phase 8).** `ConfidenceMetricsService` computes
reliability metrics from the repeated observations of a `CONFIDENCE`
scan. These metrics quantify how stable a provider's responses are
across repeated executions of the same Prompt × Provider cell.

### Methodology version

```
CONFIDENCE_METHODOLOGY_VERSION = "repeat-reliability-v1"
```

This is **NOT a statistical confidence interval**. It is a deterministic
heuristic that classifies the reliability of repeated observations into
discrete levels based on coverage and stability.

### Metric definitions

#### Measurement coverage

```
measurement_coverage = succeeded_observations / planned_observations
```

- `planned_observations`: total `PromptRun` rows for the cell
  (`repeat_count`).
- `succeeded_observations`: `SUCCEEDED` runs only.
- **Range**: [0.0, 1.0]
- Failed observations reduce coverage; they are not re-executed.

#### Repeat sufficiency

```
repeat_sufficiency = succeeded_observations / repeat_count
```

Indicates whether enough repeated observations succeeded to make a
reliability claim. Low sufficiency means too many observations failed
to draw conclusions.

#### Mention stability

For a given entity, mention stability measures how consistently the
entity is mentioned across repeated observations of the same cell:

```
mention_stability = observations_with_mention / succeeded_observations
```

- `observations_with_mention`: succeeded observations where the entity
  was mentioned.
- **Range**: [0.0, 1.0]
- `1.0` means the entity was mentioned in every successful observation.
- `0.0` means the entity was never mentioned across successful
  observations.

#### Round visibility

Round visibility reports whether the entity was visible (mentioned) in
each individual observation round:

| Field | Description |
|-------|-------------|
| `observation_index` | The round number (1..`repeat_count`) |
| `visible` | Boolean — entity was mentioned in this observation |

#### Observed visibility range

Across all succeeded observations for a cell, the observed visibility
range captures the spread of mention counts or visibility rates:

| Field | Description |
|-------|-------------|
| `min_visibility` | Lowest visibility rate across observations |
| `max_visibility` | Highest visibility rate across observations |
| `visibility_range` | `max_visibility - min_visibility` |

A range of `0.0` means perfectly stable visibility; a large range
indicates high variability between rounds.

### MeasurementConfidenceLevel

`ConfidenceMetricsService` classifies each cell into a heuristic
confidence level:

| Level | Meaning |
|-------|---------|
| `INSUFFICIENT` | Too few succeeded observations to make any claim |
| `LOW` | Minimal repeats succeeded; reliability claim is weak |
| `MEDIUM` | Moderate repeats succeeded with acceptable stability |
| `HIGH` | Many repeats succeeded with high stability |

The level is determined by a combination of measurement coverage, repeat
sufficiency, and mention stability. The default of 3 repeats can reach
at most `MEDIUM`. `HIGH` requires **>= 5 repeats** (`repeat_count >= 5`),
ensuring that high-confidence claims are backed by sufficient
observations.

## See also

- `docs/DETECTION_ENGINE.md` — detection rules and analysis lifecycle
- `docs/DATABASE.md` — analysis schema and table definitions

## Phase 8.1: Metrics Integrity and Analysis Readiness

### Analysis Required Before Metrics

Both `VisibilityMetricsService` and `ConfidenceMetricsService` now
require a `COMPLETED` `ScanAnalysis` before computing any mention-based
metrics. A missing, `PENDING`, `RUNNING`, or `FAILED` analysis is NOT
evidence that a brand was absent — the services fail closed with
`ConflictError("Scan analysis is not completed.")`.

This prevents the false-zero bug where:
- analysis failed → `mentioned_run_ids = empty` → `visibility = 0%`

That was scientifically wrong. Now:
- analysis missing/failed → metric unavailable (ConflictError)
- analysis completed with zero mentions → `0%` (true measured zero)

### True Zero vs No Measurement vs Analysis Not Ready

| Scenario | `visibility_rate` | Behavior |
|----------|-------------------|----------|
| COMPLETED analysis, 0 mentions | `0.0` | True measured zero |
| COMPLETED analysis, 0 successful obs | `None` | No measurement |
| Missing/FAILED/PENDING/RUNNING analysis | N/A | `ConflictError` |

### Provider Isolation

Provider breakdown metrics are fully provider-scoped:

- **Brand visibility**: numerator = SUCCEEDED runs from provider P that
  mention BRAND, intersected with P's successful run IDs. Never divides
  mentions from all providers by one provider's successes.
- **Round validity**: a round is valid for provider P only if P has
  successful observations in that round. P does not inherit another
  provider's valid rounds.
- **Visibility range**: `observed_visibility_min`/`max` come only from
  P's round visibility, not an overall multi-provider range.
- **Confidence level**: uses provider-specific coverage, valid rounds,
  repeat sufficiency, and BRAND mention stability.
- **Mention stability**: calculated from P's cells only.

### Brand Snapshot Identification

Provider breakdown uses `TrackedEntityType.BRAND` (Phase 7 enum), not
the legacy `"PRIMARY_BRAND"` string. Comparison is robust to SQLAlchemy
returning either the enum object or the string-backed value.

## Competitor Explanation Metrics (Phase 9)

**IMPLEMENTED (Phase 9).** `CompetitorExplanationService` computes
evidence-based brand vs competitor comparison metrics from persisted
STANDARD scan analysis evidence. These metrics are zero-cost (no AI
Checks, no provider calls) and use only persisted evidence.

### Visibility and Gap Metrics

| Metric | Description |
|--------|-------------|
| `brand_visibility_rate` | Percentage of SUCCEEDED runs mentioning the brand |
| `competitor_visibility_rate` | Percentage of SUCCEEDED runs mentioning the competitor |
| `visibility_gap_pp` | `competitor_visibility_rate - brand_visibility_rate` (percentage points) |
| `brand_share_of_voice` | Brand mentions / total mentioned presences |
| `competitor_share_of_voice` | Competitor mentions / total mentioned presences |

### Overlap Matrix

Classifies SUCCEEDED runs into four mutually exclusive buckets:

| Bucket | Meaning |
|--------|---------|
| `brand_only_runs` | Brand mentioned, competitor not |
| `competitor_only_runs` | Competitor mentioned, brand not |
| `both_runs` | Both mentioned |
| `neither_runs` | Neither mentioned |

Reconciliation invariant: `brand_only + competitor_only + both + neither = successful_observations`.

### Owned Citation Metrics

| Metric | Description |
|--------|-------------|
| `brand_owned_citation_rate` | Percentage of WEB_GROUNDED runs with brand-domain citations |
| `competitor_owned_citation_rate` | Percentage of WEB_GROUNDED runs with competitor-domain citations |
| `citation_gap_pp` | `competitor_owned_citation_rate - brand_owned_citation_rate` |

Citation metrics are computed only for `WEB_GROUNDED` runs (MODEL_ONLY
runs have no citations). Domain attribution uses
`SourceAttribution` records from the analysis phase.

### Provider Breakdown

Per-provider brand vs competitor comparison with the same visibility,
citation, and gap metrics, scoped to a single provider's runs.

### Prompt Gaps

Prompts where the competitor appears and the brand does not. Sorted by
priority: PURCHASE+commercial > PURCHASE > CONSIDERATION+commercial >
CONSIDERATION > AWARENESS, then by provider count (desc).

### Reliability Context

Optional `ReliabilityContext` from a linked `CONFIDENCE` scan (matched
by `baseline_scan_id`). Provides `overall_visibility_rate`,
`mention_stability`, `repeat_sufficiency`, observed visibility range,
and `confidence_level` for the brand entity.

### True Zero vs NULL

A `0.0000` visibility rate with a COMPLETED analysis is a true measured
zero (the brand was never mentioned in any SUCCEEDED run). `None` means
no SUCCEEDED runs exist (denominator is zero). This distinction is
preserved in all competitor explanation metrics.

See `docs/COMPETITOR_EXPLANATIONS.md` for full details.

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

## See also

- `docs/DETECTION_ENGINE.md` — detection rules and analysis lifecycle
- `docs/DATABASE.md` — analysis schema and table definitions

# Metrics

## Status

**PLANNED** (Phases 7–10). The Phase 1 data model establishes the
structures that metrics will be computed from (`prompts`, `usage_events`,
project/competitor relationships), but metric computation is not yet
implemented.

## Primary metrics

| Metric | Description | Status |
|--------|-------------|--------|
| Non-Branded Visibility | Primary visibility metric (NON_BRANDED prompts) | PLANNED |
| Branded Visibility | Tracked separately (BRANDED prompts) | PLANNED |
| Citation Rate | How often the user's domain is cited | PLANNED |
| Share of Voice | Brand mentions vs. all tracked competitors | PLANNED |
| Average Mention Position | Relative brand ordering in responses | PLANNED |
| Provider Visibility | Visibility broken down by AI engine | PLANNED |
| Competitor Gap | Difference between user and competitor visibility | PLANNED |
| Historical Visibility Change | Changes over time | PLANNED |
| Funnel Visibility | Visibility by AWARENESS / CONSIDERATION / PURCHASE | PLANNED |
| Mention Consistency | For Confidence Scans | PLANNED |

## Critical rules

- **Non-Branded Visibility is the primary metric.** Branded prompts can
  artificially inflate visibility, so they are measured separately.
- **Share of Voice defaults to NON_BRANDED prompts.** Branded and
  competitor prompts have their own breakdowns.
- Metrics must be **deterministic and documented**.
- Never present statistical certainty the data does not justify. Use
  explainable wording (e.g. "Estimated visibility: 38–46%").
- Confidence scans produce `Mention Consistency` (e.g. 67% over N runs),
  not a single absolute number.

## Prompt type classification (IMPLEMENTED in Phase 1)

Every prompt is classified as one of:

| Type | Example |
|------|---------|
| `NON_BRANDED` | "What is the best CRM for a small business?" |
| `BRANDED` | "Is HubSpot a good CRM?" |
| `COMPETITOR` | "HubSpot vs Salesforce for a small company" |

This classification is stored on `prompts.prompt_type` and is the
foundation for separating the primary visibility metric from branded
distortion.

## Prompt versioning (IMPLEMENTED in Phase 1)

`prompts.prompt_set_version` ensures historical scans remain linked to
the exact prompts used at the time. Regeneration creates a NEW version;
previous prompts are never overwritten. This is critical for measurement
consistency.

## Future models (PLANNED)

The following tables will be added in later phases to support metric
computation:

- `scans` (Phase 6)
- `prompt_runs` (Phase 6)
- `response_sources` / citations (Phase 7)
- `brand_mentions` (Phase 7)
- `opportunities` (Phase 9)

See `docs/ARCHITECTURE.md` for the roadmap.

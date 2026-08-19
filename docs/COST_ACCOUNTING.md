# Provider Cost Accounting

## Status and boundary

**IMPLEMENTED (Phase 6).** Provider cost accounting is internal and distinct from customer AI Check accounting:

- **Customer quota:** one successfully recorded `PromptRun` = one AI Check.
- **Provider economics:** tokens, cache traffic, reasoning/citation tokens, web-search requests, and request fees can all affect the provider's cost for that one run.

Internal searches therefore affect provider cost without increasing customer AI Checks. Failed executions create no `UsageEvent`, consume zero AI Checks, and have no durably attributed `cost_usd`, even though a provider may have charged GEO Tracker in an ambiguous worker-loss case.

## Money and unknown-value semantics

All money arithmetic uses Python `Decimal`; persisted amounts use `NUMERIC(18,10)`. Binary floating point is never used.

`NULL` means **unknown or unavailable**, while decimal zero means a known zero charge. The same distinction applies to usage: token/search field `NULL` means not reported; `0` means explicitly reported as zero. The calculator never substitutes fake zero for missing billable usage.

`CostSource` describes the authority for canonical `cost_usd`:

| Value | Meaning |
|---|---|
| `PROVIDER_REPORTED` | Provider supplied a valid non-negative finite request cost. |
| `PRICE_RULE` | GEO Tracker completely calculated cost from one exact effective rule and reported usage. |
| `UNKNOWN` | Complete cost cannot be established; `cost_usd` is `NULL`. |

A calculation is all-or-nothing. If a configured nonzero rate requires a usage field the provider did not report, or search usage is known to have occurred but its billable count is missing, canonical cost remains unknown rather than becoming an underestimate.

## Cost precedence

`ProviderCostCalculator` first validates any provider-reported amount and also attempts a local calculation for comparison/traceability.

1. A valid provider-reported cost **wins** and `CostSource=PROVIDER_REPORTED`.
2. Otherwise, a complete exact-rule calculation wins and `CostSource=PRICE_RULE`.
3. Otherwise, `CostSource=UNKNOWN` and monetary fields are `NULL`.

This is especially important for Perplexity Sonar: the adapter parses `usage.cost.total_cost`, or sums the available cost components when no total is supplied. A valid Perplexity provider-reported cost is authoritative over a local rule calculation. Perplexity is consequently exempt from the preflight requirement for a local price rule, although an exact rule may still be used to retain a complete parallel calculated amount.

## Exact, append-only `ProviderPriceRule`

Rules are historical pricing evidence, not mutable application constants. Resolution requires an **exact** match on:

```text
(provider, provider_surface, requested_model, effective time range)
```

There is no fuzzy model prefix, family fallback, consumer-product assumption, or "closest current model" lookup. Exactly one rule must cover the run's `started_at` (or recording time fallback). No rule means unavailable pricing; overlapping matches are a configuration error.

Each append-only rule records:

- a unique operator-defined `pricing_key`;
- exact provider, surface, and model ID;
- `effective_from` inclusive and `effective_to` exclusive (or open-ended);
- per-million input, cached-input, cache-write, output, reasoning, and citation token rates;
- per-1,000 search-request rate and per-request fee;
- whether input usage includes cached tokens and output usage includes reasoning tokens;
- `verified_at`, official `source_url`, and notes.

Rates are nullable because provider schemas differ. Existing rules referenced by `PromptRun` or `UsageEvent` are protected by `ON DELETE RESTRICT`. Do not edit a historical row when pricing changes: close its effective interval and append a newly verified exact rule. Operators must ensure intervals do not overlap; runtime refuses ambiguous resolution.

## Calculation outline

For a complete rule, cost is the Decimal sum of applicable components:

```text
billable input tokens × input rate / 1,000,000
+ cached input tokens × cached rate / 1,000,000
+ cache-write tokens × cache-write rate / 1,000,000
+ billable output tokens × output rate / 1,000,000
+ reasoning tokens × reasoning rate / 1,000,000 (when separately billed)
+ citation tokens × citation rate / 1,000,000
+ search requests × search rate / 1,000
+ request fee
```

The inclusion flags prevent double charging cached or reasoning tokens when provider aggregate fields already include them. Impossible relationships (for example cached input greater than total input) make the local calculation incomplete.

## Atomic ledger recording

A successful result records the following atomically in the same transaction:

- `PromptRun` evidence, normalized usage, calculated/provider/canonical costs, source, rule, and status;
- exactly one immutable `UsageEvent` with the same accounting values;
- ordered `ResponseSource` rows;
- transfer of one AI Check from reserved to used.

`UsageEvent` is idempotent on `prompt-run:{prompt_run_id}:usage`; all material fields are compared on reuse. One-to-one unique foreign keys connect the run and usage event. A failure rolls back the whole transaction, preventing evidence without accounting or accounting without evidence.

Customer Scan API schemas intentionally omit all cost, token, pricing-rule, usage-event, and reservation fields. These values are retained internally for operations, reconciliation, and financial analysis.

## Production catalog responsibility

**The Phase 6 migration intentionally adds no real production `ProviderPriceRule` rows.** Configured scan models are environment-driven and default to empty strings, so the repository cannot safely seed exact model pricing. Inventing defaults or generic family rates would violate exact resolution.

Before enabling a non-Perplexity provider with `PRICING_REQUIRE_RULE_FOR_EXECUTION=true` (the default), an operator must:

1. choose the exact deployed model ID from the environment;
2. verify all applicable token, cache, tool/search, reasoning/citation, and request charges in official documentation;
3. determine the provider's usage inclusion semantics;
4. insert an exact, non-overlapping, append-only rule with `verified_at` and official `source_url`;
5. repeat the process with a new effective rule whenever model or pricing changes.

Do not copy example rates from documentation into production without re-verification.

## Official pricing references

Official documentation was last verified **2026-08-19**. These links are evidence sources, not hardcoded runtime pricing:

### OpenAI

- Pricing: https://developers.openai.com/api/docs/pricing
- Prompt caching: https://developers.openai.com/api/docs/guides/prompt-caching
- Web search: https://developers.openai.com/api/docs/guides/tools-web-search
- Deep research: https://developers.openai.com/api/docs/guides/deep-research

OpenAI WEB_GROUNDED also sends configurable `OPENAI_WEB_SEARCH_MAX_TOOL_CALLS` (default 3), which bounds provider tool calls and can affect provider cost; it does not alter the one-AI-Check customer rule.

### Anthropic

- Pricing: https://docs.anthropic.com/en/docs/about-claude/pricing
- Prompt caching: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
- Web search: https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/web-search-tool

### Google

- Gemini API pricing: https://ai.google.dev/gemini-api/docs/pricing
- Thinking: https://ai.google.dev/gemini-api/docs/thinking
- Interactions API: https://ai.google.dev/gemini-api/docs/interactions

Google is `MODEL_ONLY` in GEO Tracker; do not configure Google Search grounding rates as if that surface were executed.

### Perplexity

- Pricing: https://docs.perplexity.ai/docs/getting-started/pricing
- Models: https://docs.perplexity.ai/docs/sonar/models
- Sonar API: https://docs.perplexity.ai/api-reference/sonar-post

See [Scan Engine](SCAN_ENGINE.md) for lifecycle and quota behavior.

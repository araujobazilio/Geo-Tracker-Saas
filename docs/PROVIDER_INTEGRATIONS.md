# Provider Integrations

## Overview

This document describes the **Phase 5 AI Provider Abstraction and Integrations**
layer of GEO Tracker. The provider layer is the single boundary between the
application and the external AI companies whose surfaces are being measured.

The abstraction is intentionally thin: it normalizes request/response shapes
across heterogeneous vendor APIs while preserving the experimental integrity of
the prompt and the reproducibility of the measurement. It deliberately does
**not** own quota, pricing, retries, or any policy that belongs to the Phase 6
Scan Engine.

> **Official provider docs last verified: 2026-08-19**

---

## Provider vs Provider Surface

A **Provider** is the company that operates the API:

| Provider   | Company     |
|------------|-------------|
| `OPENAI`   | OpenAI      |
| `ANTHROPIC`| Anthropic   |
| `GOOGLE`   | Google      |
| `PERPLEXITY`| Perplexity |

A **Provider Surface** is the specific API endpoint/protocol being measured:

| Provider Surface            | Protocol                          |
|-----------------------------|-----------------------------------|
| `OPENAI_RESPONSES_API`      | OpenAI Responses API              |
| `ANTHROPIC_MESSAGES_API`    | Anthropic Messages API            |
| `GOOGLE_INTERACTIONS_API`   | Google Interactions API           |
| `PERPLEXITY_SONAR_API`      | Perplexity Sonar API              |

API results are measurements of their **named provider surfaces**, NOT
necessarily identical to consumer-facing products such as ChatGPT, Claude.ai,
the Gemini UI, or the Perplexity UI. Consumer products may use different
models, routing, personalization, safety post-processing, or UI-only features
that are not exposed through the public API. Documentation and reports must
always refer to the surface, not the consumer product.

---

## Execution Modes

Each measurement runs in exactly one execution mode:

- **`MODEL_ONLY`** — Send the prompt to the model without any web search tool.
  Measures the model's parametric knowledge and reasoning.
- **`WEB_GROUNDED`** — Use the provider's native web search tool to ground the
  answer in fresh web content. Measures retrieval-augmented generation.

### Provider support matrix

| Provider   | `MODEL_ONLY` | `WEB_GROUNDED`              |
|------------|--------------|-----------------------------|
| OpenAI     | Yes          | Yes                         |
| Anthropic  | Yes          | Yes                         |
| Google     | Yes          | No (compliance restriction) |
| Perplexity | No           | Yes                         |

Modes that a provider does not support are rejected **before** any network call
with `ProviderModeNotAllowedError` (see [Error Taxonomy](#error-taxonomy)).

---

## Provider Abstraction

The abstraction is built from the following core types.

### `ProviderAdapter` protocol

The protocol implemented by every provider integration:

- `async execute(request: ProviderRequest) -> ProviderResult`
- `capabilities() -> ProviderCapabilities` — declared **without** any API call

Adapters are the only components that know how to speak a specific vendor
protocol. Everything above the adapter works in normalized types.

### `ProviderRequest`

Immutable input to an adapter:

- Preserves the **prompt text exactly** as supplied (no normalization, no
  trimming, no rewriting).
- Carries the requested execution mode (`MODEL_ONLY` / `WEB_GROUNDED`).
- Carries the requested model ID.

### `ProviderResult`

Immutable normalized output from an adapter:

- Final answer text.
- Citations (normalized to `ProviderCitation`).
- Usage (`ProviderUsage`).
- `requested_model` and `returned_model` (see
  [Model ID Reproducibility](#model-id-reproducibility)).
- `latency_ms` (see [Latency](#latency)).
- `provider_request_id`: an HTTP request/support identifier surfaced by the
  provider (e.g. the `x-request-id`, `request-id`, or `X-Request-ID` response
  header). `None` when the provider does not supply one.
- `provider_response_id`: a generated resource/object identifier returned in
  the response body (e.g. response ID, message ID, interaction ID, completion
  ID).
- `provider_reported_cost_usd`: a `Decimal` cost as reported **by the
  provider** in its own usage/cost payload. This is **not** GEO Tracker's own
  pricing; it is the vendor's accounting of the request. `None` when the
  provider does not report a cost.

### `ProviderUsage`

Normalized token accounting. The `None` vs `0` distinction is semantically
significant:

- **`None`** — the field was **not reported** by the provider.
- **`0`** — the field was **reported as zero** by the provider.

Callers must not coerce `None` to `0`; doing so destroys the distinction
between "unknown" and "measured zero".

Fields include `citation_tokens: int | None` (reported by Perplexity; the
token cost attributable to cited content). All other fields follow the same
`None` vs `0` convention.

### `ProviderCitation`

Normalized citation shape, mapped from each provider's native citation format
(see per-provider sections below). Adapters are responsible for this
normalization so that downstream code never branches on provider.

### `ProviderCapabilities`

Declares what a provider can do **without** making any API call. This lets the
application reject unsupported modes and surface capability metadata to users
without spending quota or tokens.

### `ProviderRegistry`

- Adapters are constructed **lazily** on first request for a given provider.
- A **missing provider** (e.g. no API key configured) does **not** crash
  application startup. The provider is simply unavailable; requests for it
  fail at execution time with a configuration error.
- **`capabilities()` works WITHOUT credentials.** It returns static adapter
  facts (supported modes, surface name) and makes **zero network calls**, so
  capability metadata is always available even when no API key is configured.

---

## No Hidden System-Prompt Distortion

Cross-provider measurement comparability depends on every provider receiving
the **same experimental input**.

- The **user prompt itself is the experimental input**.
- Adapters perform only the **minimum provider-specific envelope** required by
  the protocol (e.g. the `messages` array shape, required headers, tool
  declarations for `WEB_GROUNDED`).
- There are **no different semantic system prompts** for different providers.
  Any system-level text required purely for protocol compliance is identical
  across providers and does not alter the meaning of the user prompt.
- The prompt is never paraphrased, expanded, or "preprocessed" per provider.

This preserves the invariant that differences in results are attributable to
the provider surface, not to adapter-induced prompt distortion.

---

## No Automatic Retries

- **One `execute()` call = at most ONE billable provider request.**
- Adapters never retry on their own. Transient failures surface as the
  appropriate `Provider*Error`.
- The **Phase 6 Scan Engine owns retry policy** (backoff, jitter, attempt
  budget, idempotency).
- Tests verify that the **transport call count is exactly 1** per `execute()`,
  including on error paths.

---

## Quota Boundary

- Provider adapters do **NOT** call `QuotaService`.
- Provider adapters do **NOT** create `UsageEvent` records.
- The **Phase 6 Scan Engine owns quota reservation and accounting**. The
  adapter's only job is to execute the request and return a normalized
  `ProviderResult` (or raise).

---

## Pricing Boundary

- There is **no hardcoded provider token/search pricing** in the adapter layer.
- **Phase 6 will implement a versioned pricing snapshot** that maps provider,
  model, and surface to costs at a point in time.
- Adapters never emit **fake `cost_usd` values**. If a cost is not derivable
  from a real pricing snapshot, it is simply absent.

---

## API-Key Handling

API keys are server-side secrets and are treated accordingly:

- **Server environment configuration only.** Keys are read from environment
  variables / settings, never from the database.
- Stored as **`SecretStr`** in Settings so they are not accidentally rendered.
- **Never persisted in the database.**
- **Never sent to the browser** (no API surface exposes them).
- **Never included in `repr`, logs, or exceptions.** Errors raise typed
  `Provider*Error` instances without embedding the key.
- **BYOK (Bring Your Own Key)** is deferred to a later phase; the current
  design assumes platform-managed keys only.

---

## Provider Adapters

### OpenAI (`OPENAI_RESPONSES_API`)

- **Endpoint:** `POST /responses` (current Responses API).
- **`MODEL_ONLY`:** no tools supplied.
- **`WEB_GROUNDED`:** `tools=[{"type": "web_search"}]` and
  `tool_choice={"type": "web_search"}` to force web search.
- **`include`:** `["web_search_call.action.sources"]` is requested so that
  source metadata is returned alongside the response.
- **`store=false`** is set on every request for privacy and reproducibility
  (no server-side retention of inputs/outputs by the provider).
- **`max_output_tokens`** is sent in the request body.
- **Citations:** normalized from **both** inline `url_citation` annotations
  **and** `web_search_call.action.sources`, deduplicated.
- **Usage:** `input_tokens`, `output_tokens`, `total_tokens`;
  `input_tokens_details.cached_tokens` → `cached_input_tokens`;
  `output_tokens_details.reasoning_tokens` → `reasoning_tokens`.
- **Search count:** `search_requests` = the number of `web_search_call` items
  in the response.
- **`WEB_GROUNDED` without an observed search** → `ProviderSearchError`.
- **IDs:** `provider_request_id` = the `x-request-id` HTTP response header;
  `provider_response_id` = the `id` field in the response JSON.

### Anthropic (`ANTHROPIC_MESSAGES_API`)

- **Endpoint:** `POST /v1/messages` (current Messages API).
- **Headers:** `x-api-key` and `anthropic-version: 2023-06-01`.
- **`MODEL_ONLY`:** no tools supplied.
- **`WEB_GROUNDED`:**
  `tools=[{"type": "<version>", "name": "web_search", "max_uses": 5}]` and
  `tool_choice={"type": "tool", "name": "web_search"}` to force web search.
- **Default tool version:** `web_search_20260318` (configurable). Available
  versions: `web_search_20250305`, `web_search_20260209`, `web_search_20260318`.
- **Configurable via settings:**
  - `ANTHROPIC_WEB_SEARCH_TOOL_VERSION` (the `type` suffix, e.g. `20260318`)
  - `ANTHROPIC_WEB_SEARCH_MAX_USES` (the `max_uses` value)
- **`max_tokens`** is sent in the request body.
- **Body-level search error detection:** a `web_search_tool_result_error`
  field in the response body is mapped to `ProviderSearchError` rather than
  being treated as a successful empty answer.
- **`stop_reason: "pause_turn"`** → `ProviderSearchError`. This indicates an
  incomplete server-side search loop, **not** a final answer. There is **no
  automatic continuation** on `pause_turn` (continuing would be another
  billable request, violating the one-call-per-execute invariant).
- **`WEB_GROUNDED` without an observed search** → `ProviderSearchError`.
- **Citations:** normalized from `web_search_result_location` blocks.
- **Usage:** `input_tokens`, `output_tokens`, and
  `server_tool_use.web_search_requests`;
  `cache_read_input_tokens` → `cached_input_tokens`;
  `cache_creation_input_tokens` → `cache_write_input_tokens`.
- **IDs:** `provider_request_id` = the `request-id` HTTP response header;
  `provider_response_id` = the `id` field in the message JSON.

### Google (`GOOGLE_INTERACTIONS_API`)

- **Endpoint:** `POST {base_url}/v1beta/interactions` (current Interactions
  API — **not** `generateContent`).
- **Request body:**
  `{"model": model, "input": prompt, "store": false, "generation_config": {"max_output_tokens": N}}`.
- **`store=false`** for stateless one-shot measurements (no server-side
  retention of the interaction).
- **`MODEL_ONLY` only** — this is a compliance restriction.
- **`WEB_GROUNDED` → `ProviderModeNotAllowedError` BEFORE any network call.**
- **Response:** JSON with `id` (interaction ID), `status`, a `steps` array,
  and a `usage` object.
  - Only `model_output` steps are parsed for text; `thought` steps are
    **discarded**.
  - **Status values:** `completed`, `failed`, `cancelled`, `requires_action`,
    `incomplete`, `budget_exceeded`. Any **non-`completed`** status →
    `ProviderResponseError`.
- **Usage fields:** `total_input_tokens`, `total_output_tokens`,
  `total_tokens`, `total_cached_tokens`, `total_thought_tokens`.
- **IDs:** `provider_response_id` = the interaction `id`;
  `provider_request_id` = `None` (Google does not provide a standard HTTP
  request ID).
- **Important:** This is a measurement of the **Interactions API surface**,
  **NOT** a measurement of Google AI Overviews. AI Overviews is a separate
  consumer product with its own retrieval and ranking pipeline that is not
  exposed via this API.

### Perplexity (`PERPLEXITY_SONAR_API`)

- **Endpoint:** `POST /v1/sonar` (current Sonar API).
- **`WEB_GROUNDED` only** — Sonar always searches.
- **`MODEL_ONLY` → `ProviderModeNotAllowedError` BEFORE any network call.**
- **`max_tokens`** is sent in the request body.
- **Citations:**
  - `search_results` is the **primary** source for citations.
  - `citations` (legacy array) is a **fallback** when `search_results` is
    absent.
- **Usage:** `prompt_tokens`, `completion_tokens`, `total_tokens`,
  `num_search_queries`, plus `citation_tokens` and `reasoning_tokens` (now
  parsed when reported).
- **`provider_reported_cost_usd`:** a `Decimal` parsed from
  `usage.cost.total_cost` — this is the **provider-reported** cost, **not**
  GEO Tracker's own pricing. The cost object fields are:
  `input_tokens_cost`, `output_tokens_cost`, `reasoning_tokens_cost`,
  `request_cost`, `citation_tokens_cost`, `search_queries_cost`,
  `total_cost`.
- **IDs:** `provider_request_id` = the `X-Request-ID` HTTP response header;
  `provider_response_id` = the `id` field in the response JSON (completion
  ID).

---

## Error Taxonomy

All adapter failures raise one of the following typed errors. None of them
embed API keys or raw request bodies.

| Error                          | Meaning                                                        |
|--------------------------------|----------------------------------------------------------------|
| `ProviderError`                | Base class for all provider errors.                            |
| `ProviderConfigurationError`   | Missing/invalid configuration (e.g. empty `SCAN_MODEL`).       |
| `ProviderAuthenticationError`  | HTTP 401/403 — bad or revoked credentials.                     |
| `ProviderRateLimitError`       | HTTP 429, with `retry_after_seconds` when the provider sends it. |
| `ProviderTimeoutError`         | Request exceeded the configured timeout.                       |
| `ProviderUnavailableError`     | HTTP 5xx — provider-side outage.                               |
| `ProviderBadRequestError`      | HTTP 400 — malformed request to the provider.                  |
| `ProviderResponseError`        | Response was malformed or empty (not an HTTP status error).    |
| `ProviderSearchError`          | Search-tool failure or missing search: body-level search tool failure (e.g. Anthropic's `web_search_tool_result_error`), `WEB_GROUNDED` with no observed search, or Anthropic `stop_reason: "pause_turn"` (incomplete server-side search loop). |
| `ProviderModeNotAllowedError`  | The requested execution mode is not supported by the provider. |

`ProviderModeNotAllowedError` is raised **before** any network call so that no
quota is consumed and no billable request is made for an unsupported mode.

---

## Configuration

All provider settings are environment-driven (see `.env.example`).

- **Timeouts:**
  - `PROVIDER_CONNECT_TIMEOUT_SECONDS`
  - `PROVIDER_READ_TIMEOUT_SECONDS`
- **Base URLs:** configurable per provider; defaults point to the official
  APIs. This supports testing against recorded/mocked transports.
- **Empty `SCAN_MODEL`:** when a provider is requested but no model is
  configured, the adapter raises `ProviderConfigurationError`.
- **Missing provider key:** does **NOT** crash application startup. The
  provider is registered as unavailable and fails per-request with
  `ProviderConfigurationError` when invoked.

---

## HTTP Client Strategy

- **`httpx.AsyncClient`** is used for all providers.
- **No provider SDKs** are used. SDKs hide protocol details, add version
  coupling, and often perform hidden retries that violate the
  one-call-per-execute invariant.
- **`MockTransport`** is used for deterministic tests.
- **No external internet access** is required in CI; all provider tests run
  against mocked transports.

---

## Model ID Reproducibility

- `ProviderResult` preserves both `requested_model` (what was configured and
  sent) and `returned_model` (what the provider reported it actually ran, when
  available).
- Historical scans therefore **remain explainable** even if the configured
  model changes later. A past scan records exactly which model was requested
  and, where the provider reports it, which model was used.

---

## Latency

- Latency is measured with the **monotonic clock** (`time.monotonic`), which is
  immune to system clock adjustments.
- The measured value is reported as `latency_ms` in `ProviderResult`.
- The interval covers the single billable provider request only, not any
  adapter-internal setup that does not touch the network.

---

## No Chain of Thought

- Adapters **never expose or persist** reasoning/thinking blocks, even when a
  provider returns them.
- Only the **final answer**, **citations**, **usage**, and **request IDs** are
  stored.
- This keeps the measurement focused on the observable answer and avoids
  storing intermediate reasoning that providers may consider sensitive or that
  may change without notice.

---

## WEB_GROUNDED Search Invariant

A `WEB_GROUNDED` measurement is only valid if a web search actually occurred.

- **`WEB_GROUNDED` success ALWAYS implies `search_used == True`.**
- If a `WEB_GROUNDED` request completes without any observed search activity
  (no search tool call, no search results, no citations traceable to a
  search), the adapter raises `ProviderSearchError` rather than returning a
  result with `search_used == False`.
- This is a **critical integrity guarantee** for GEO measurement: a
  "web-grounded" answer that was not actually grounded in fresh web content
  would be indistinguishable from a `MODEL_ONLY` answer and would corrupt the
  experiment.

---

## Malformed JSON Handling

- All four adapters normalize invalid JSON responses to
  `ProviderResponseError`.
- `json.JSONDecodeError`, `ValueError`, and `httpx` internal decode errors
  are caught and wrapped; they **never leak** to callers as raw stdlib/httpx
  exceptions.
- This keeps the adapter boundary clean: every failure a caller sees is a
  typed `Provider*Error`, never an uncontrolled third-party exception.

---

## Output Token Limits

All four adapters transmit the configured maximum output token limit to the
provider where the protocol supports it:

| Provider   | Field sent                                              |
|------------|---------------------------------------------------------|
| OpenAI     | `max_output_tokens` in the request body                 |
| Anthropic  | `max_tokens` in the request body                        |
| Google     | `generation_config.max_output_tokens`                   |
| Perplexity | `max_tokens` in the request body                        |

This keeps the measurement budget explicit and reproducible across runs.

# Isolated live provider validation

The internal validation operator is a narrow operational path for one
explicitly authorized provider observation. It is not a public API and does
not replace the normal scan engine.

## Safety contract

The operator requires an explicit workspace, validation identifier, UTF-8
prompt file, and evidence directory. It refuses the protected WhiteboardMaker
workspace/project/PromptSet and permits only one `OPENAI` target. The normal
customer scan creation and prompt-generation services are not changed to
accept the operator generator identity:

`operator-live-validation-v1`

The plan is exactly one Prompt, one OpenAI PromptRun, one AI Check reservation,
and one synchronous call through `ScanExecutionService.execute_scan`. It does
not dispatch Celery work, start a worker, retry, calculate cost, or call
`pricing --apply`. Pricing and quota remain the existing product services.

## Plan-only invocation

Plan mode is the default and performs local input plus database preflight only.
It never creates validation entities or calls a provider.

```text
python -m scripts.live_provider_validation \
  --workspace-id <explicit-workspace-uuid> \
  --validation-id validation-2026-01 \
  --prompt-file <exact-utf8-prompt-file> \
  --evidence-dir <dedicated-evidence-directory> \
  --provider OPENAI
```

## Explicit live invocation

Live mode requires the exact acknowledgement below in addition to all explicit
arguments:

`I UNDERSTAND THIS WILL MAKE ONE PAID OPENAI CALL`

```text
python -m scripts.live_provider_validation \
  --workspace-id <explicit-workspace-uuid> \
  --validation-id validation-2026-01 \
  --prompt-file <exact-utf8-prompt-file> \
  --evidence-dir <dedicated-evidence-directory> \
  --provider OPENAI \
  --execute-live \
  --acknowledge-paid-provider-call "I UNDERSTAND THIS WILL MAKE ONE PAID OPENAI CALL"
```

There are deliberately no retry, rerun, or force flags. A validation ID is
idempotent. If a durable scan already exists, the operator returns its state
and never invokes the provider again; an ambiguous `RUNNING` state is blocked
for manual reconciliation.

The integration coverage reuses a validation ID serially because the test
fixture is transaction-scoped and is not concurrency evidence. Concurrent
production attempts remain guarded by the database uniqueness/locking rules
for the scan and quota idempotency keys; SQLite-style test results must not be
used to weaken that guarantee.

## Evidence

The evidence directory is prepared before network I/O. The filesystem sink
writes atomically, with directory mode `700` and artifact mode `600` on the
POSIX live-execution platform:

- `request.json` — the sanitized request body without authorization headers;
- `response_transport.json` — status, request ID, byte length, latency, and a
  SHA-256 fingerprint of the pre-parse HTTP response bytes;
- `response.json` — the complete parsed response envelope after recursive
  sanitization;
- `web_actions.json` — ordered sanitized action types;
- `manifest.json` — correlation metadata plus a filename, semantic role, byte
  size, and SHA-256 for every content artifact;
- `manifest.sha256` — detached SHA-256 of the exact persisted manifest bytes.

The transport fingerprint is deliberately not treated as the hash of
`response.json`: the former covers the bytes received before parsing, while the
latter covers the sanitized JSON bytes actually persisted. The raw HTTP body is
never written to an evidence artifact.

The response envelope preserves useful structure, including output ordering,
web actions, sources, usage, status, incomplete details, and provider IDs, but
credential-like keys and values are redacted recursively. Evidence is sensitive
operational data even after sanitization because it can contain the prompt,
provider output, URLs, sources, and usage metadata.

The fixed bundle is validated before idempotent reuse. The validator first
checks `manifest.sha256`, then validates every indexed artifact's filename,
semantic role, size, and SHA-256, and finally checks critical run correlation
IDs. Missing, extra, partial, corrupt, or mismatched files produce
`EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT`; the operator never repairs, deletes,
overwrites, or reexecutes such a bundle.

## Production storage contract

For an explicitly authorized production validation, the approved design is:

- host root: `/opt/geo-tracker/provider-validation-evidence`;
- container root: `/var/lib/geo-tracker/provider-validation-evidence`;
- an explicit read/write bind mount from the host root to the container root;
- access restricted to the one-shot operator's application user, with host
  directory mode `0700` and artifacts mode `0600`;
- the host root must be durable across app-container recreation and must not be
  the container's ephemeral filesystem.

The bind mount is a future deployment prerequisite. It is not added to
Compose, created on a host, or applied automatically by this feature. Evidence
bundles are not committed to Git, are not part of the Docker build context,
and are not included automatically in the PostgreSQL backup. They are not
deleted automatically. Before regular commercial use, a separate backup and
retention policy must be approved; for a controlled paid validation, preserve
the bundle manually until the test or incident is explicitly closed.

On POSIX, the sink opens each directory component with directory descriptors,
`O_DIRECTORY`, and `O_NOFOLLOW`, creates the run directory exclusively, uses
`O_CREAT|O_EXCL` temporary files, publishes without overwriting existing
artifacts, and fsyncs the file and directory metadata. Execute-live is not
approved on Windows merely because plan mode and offline tests run there.

The provider adapter captures the transport response before parsing and then
captures the sanitized response before HTTP/JSON/functional validation. The
Authorization headers and API keys are never intentionally written. If writing
evidence fails after a response arrives, the canonical accounting path still
processes that response exactly once and the operator reports an inconclusive
result; it never retries the paid call.

Each validation gets a hashed, collision-safe run directory, and an existing
directory, symlink, or unsafe permission state fails closed.

## Result interpretation

The operator reports persisted counters and costs; it does not derive cost
itself. `web_tool_call_count` remains the tool-bound authority and
`search_action_count` remains the billable search authority. Unknown action
types are preserved and reported as inconclusive. A missing search, an
incomplete response, and a tool-bound violation are reported distinctly.

All tests for this feature use injected adapters or `httpx.MockTransport`; no
test is permitted to call `api.openai.com`.

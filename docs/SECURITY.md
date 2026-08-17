# Security

## Status

Phase 0/1 establish foundational security primitives. Full authentication,
session handling, tenant-access enforcement, and CSRF are added in
Phase 2. Security hardening is Phase 17.

## Implemented

- **Password hashing:** Argon2id via `argon2-cffi` (`app/core/security.py`).
- **Secrets:** loaded from environment via `pydantic-settings`; `.env` is
  git-ignored; only `.env.example` is committed.
- **Production secret validation:** `APP_SECRET_KEY` is validated at config
  load time. In staging/production, empty, known-placeholder, or
  too-short (< 32 chars) secrets are rejected with a clear error. The
  real secret value is never included in error messages.
  See `app/config.py` (`Settings._validate_production_secret`).
- **Constant-time comparison:** `safe_eq` helper for sensitive comparisons.
- **Tenant data model:** the Workspace tenant boundary, membership model,
  and tenant-scoped data model are in place (see `docs/MULTITENANCY.md`).
  Authenticated tenant-access enforcement is PLANNED for Phase 2.
- **UUIDs:** public-facing identifiers are UUIDs to reduce IDOR risk.
- **Database accounting integrity:** `usage_events` has database-level
  non-negative CHECK constraints for `ai_checks`, token counts, and
  `cost_usd` (see `docs/DATABASE.md`).
- **AppSumo license uniqueness:** `external_license_id` is UNIQUE at the
  database level.
- **Logging:** structured logs never include passwords, API keys, or tokens.
- **Error handling:** application errors never expose stack traces to
  clients (see `app/core/exceptions.py`).

## Planned (Phase 2)

- HttpOnly + Secure + SameSite cookies for sessions.
- CSRF protection where relevant.
- Authenticated tenant-access enforcement (repository/service
  authorization, role enforcement, IDOR prevention).

## Planned (later phases)

- Rate limiting (Phase 17).
- Webhook signature verification + replay protection (Phase 14/15).
- Encrypted secret storage for future BYOK (Phase 17+).
- Output escaping in templates (Phase 12).

## Rules

- Never store authentication credentials or bearer tokens in `localStorage`.
- Never expose server API keys to frontend JavaScript.
- Never commit `.env`, credentials, private keys, or access tokens.
- Never log secrets, credentials, or authentication tokens.
- Never store passwords, API secrets, or auth tokens in audit logs.

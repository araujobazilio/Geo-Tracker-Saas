# Security

## Status

Phase 0/1 establish foundational security primitives. Full authentication,
session handling, and CSRF are added in Phase 2. Security hardening is
Phase 17.

## Implemented

- **Password hashing:** Argon2id via `argon2-cffi` (`app/core/security.py`).
- **Secrets:** loaded from environment via `pydantic-settings`; `.env` is
  git-ignored; only `.env.example` is committed.
- **Constant-time comparison:** `safe_eq` helper for sensitive comparisons.
- **Tenant isolation:** architectural boundary enforced at the data-access
  layer (see `docs/MULTITENANCY.md`).
- **UUIDs:** public-facing identifiers are UUIDs to reduce IDOR risk.
- **Logging:** structured logs never include passwords, API keys, or tokens.
- **Error handling:** application errors never expose stack traces to
  clients (see `app/core/exceptions.py`).

## Planned

- HttpOnly + Secure + SameSite cookies for sessions (Phase 2).
- CSRF protection where relevant (Phase 2).
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

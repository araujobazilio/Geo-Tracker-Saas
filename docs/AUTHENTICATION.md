# Authentication

## Architecture

GEO Tracker uses **opaque server-side sessions** stored in Redis. No JWT
tokens are used for browser authentication.

```
Browser → HttpOnly cookie (opaque token) → Redis session record → user_id
```

The browser cookie contains only a random, unpredictable session token.
No user data, roles, workspace IDs, or authorization claims are stored
in the cookie.

## Session token

- Generated using `secrets.token_urlsafe(32)` (256 bits of entropy).
- Never uses UUID1, predictable values, user IDs, or database IDs.
- Never stored in persistent logs.

## Session storage (Redis)

Session records are stored in Redis under a **SHA-256 hash** of the
session token, never the raw token:

```
geo:session:{sha256_hash_of_token}
```

This reduces exposure if Redis keys are inspected.

Session data stored in Redis:
- `user_id`
- `csrf_token`
- `created_at`
- `expires_at`

Redis data is NOT treated as authorization truth for workspace
membership. Workspace authorization always uses the authoritative
database membership tables.

## Session expiration

- Default TTL: **7 days** (`SESSION_TTL_SECONDS`).
- Redis TTL enforces server-side expiration.
- Cookie `Max-Age` is aligned with server-side expiration.

## Cookie security

| Attribute | Value |
|-----------|-------|
| HttpOnly | `true` |
| SameSite | `Lax` (default) |
| Secure | `true` in staging/production; `false` in development |
| Path | `/` |
| Max-Age | `SESSION_TTL_SECONDS` |
| Name | `geo_session` (configurable via `SESSION_COOKIE_NAME`) |

Production/staging always enforces `Secure=true` regardless of
configuration (fail-fast in the config validator).

## CSRF protection

Because authentication uses cookies, state-changing requests require
CSRF protection.

**Strategy:**
1. Session stores a random CSRF token (generated at session creation).
2. Browser receives the token via `GET /api/v1/auth/csrf`.
3. State-changing requests must include the token in the `X-CSRF-Token` header.
4. Server validates using constant-time comparison (`safe_eq`).

**Protected methods:** POST, PUT, PATCH, DELETE.

**Exempt paths:** `/api/v1/auth/login`, `/api/v1/auth/register`,
`/health`, `/ready` (no session exists yet or infrastructure).

GET/HEAD/OPTIONS requests do not require CSRF protection.

## Email normalization

Canonical strategy: **store and compare emails in lowercase**.

- `normalize_email()` strips whitespace and lowercases the entire address.
- `Alice@Example.com` and `alice@example.com` are treated as the same email.
- Database-level UNIQUE constraint on `users.email` prevents duplicates.

## Password policy

| Rule | Value |
|------|-------|
| Minimum length | 12 characters |
| Maximum length | 128 characters |
| Character classes | No arbitrary requirements |
| Hashing | Argon2id (time_cost=3, memory=64KB, parallelism=2) |

Passwords are never logged or returned in responses.

## Password rehash

On successful login, if the stored Argon2id parameters are outdated
(`check_needs_rehash`), the password is rehashed with current
parameters.

## Login timing

A dummy Argon2id verification is performed for nonexistent users to
reduce timing side-channels between "user does not exist" and "wrong
password". Both cases return the same generic error:
`Invalid email or password.`

## Rate limiting

Redis-backed sliding-window rate limiting for auth endpoints:

| Endpoint | Limit | Window |
|----------|-------|--------|
| Login | 8 attempts per IP | 5 minutes |
| Register | 5 attempts per IP | 1 hour |

Configurable via `RATE_LIMIT_LOGIN_MAX`, `RATE_LIMIT_LOGIN_WINDOW_SECONDS`,
`RATE_LIMIT_REGISTER_MAX`, `RATE_LIMIT_REGISTER_WINDOW_SECONDS`.

Rate limiting does NOT permanently lock users. It is a throttling
mechanism only.

## Client IP resolution

Uses `request.client.host` directly. Does NOT blindly trust
`X-Forwarded-For` headers. Reverse proxy trust configuration should be
hardened during production deployment.

## Session fixation

On successful login, a **new session token** is always issued. Previous
or attacker-controlled session identifiers are never reused.

## Concurrent sessions

Multiple sessions per user are allowed. Architecture supports future
"Log out all devices" via `SessionService.revoke_all_user_sessions()`.

## Audit events

| Event | When |
|-------|------|
| `USER_REGISTERED` | Successful registration |
| `LOGIN_SUCCEEDED` | Successful login |
| `LOGIN_FAILED` | Failed login (wrong password, nonexistent user, inactive) |
| `LOGOUT` | Successful logout |
| `WORKSPACE_CREATED` | Workspace created |
| `WORKSPACE_UPDATED` | Workspace updated |
| `TENANT_ACCESS_DENIED` | Cross-tenant access attempt (logged at service layer) |

Audit logs NEVER store: session tokens, cookie values, CSRF tokens,
passwords, or password hashes.

## API endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| POST | `/api/v1/auth/register` | Create account + default workspace | No |
| POST | `/api/v1/auth/login` | Authenticate + issue session | No |
| POST | `/api/v1/auth/logout` | Revoke session + clear cookie | Yes (CSRF) |
| GET | `/api/v1/auth/me` | Current user info + workspaces | Yes |
| GET | `/api/v1/auth/csrf` | Get CSRF token | Yes |
| GET | `/api/v1/workspaces` | List user's workspaces | Yes |
| POST | `/api/v1/workspaces` | Create workspace | Yes (CSRF) |
| GET | `/api/v1/workspaces/{id}` | Get workspace (member-only) | Yes |
| PATCH | `/api/v1/workspaces/{id}` | Update workspace (OWNER/ADMIN) | Yes (CSRF) |

## Role matrix

| Capability | OWNER | ADMIN | MEMBER |
|-----------|-------|-------|--------|
| View workspace | ✓ | ✓ | ✓ |
| Update workspace | ✓ | ✓ | ✗ |
| Manage members | ✓ | ✓ | (later) |
| Manage resources | ✓ | ✓ | (later) |

## Tenant isolation (IDOR policy)

An authenticated user can NEVER access another workspace by guessing or
obtaining its UUID. Cross-tenant access attempts return **HTTP 404**
(not 403) to avoid revealing whether an inaccessible resource exists.

Enforcement: `WorkspaceAuthorizationService.require_membership()` checks
database membership. Non-members receive `TenantAccessError` → 404.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `SESSION_COOKIE_NAME` | `geo_session` | Cookie name |
| `SESSION_TTL_SECONDS` | `604800` (7 days) | Session lifetime |
| `SESSION_COOKIE_SECURE` | `false` (enforced `true` in prod) | Secure flag |
| `SESSION_COOKIE_SAMESITE` | `lax` | SameSite attribute |
| `CSRF_COOKIE_NAME` | `geo_csrf` | CSRF cookie name (unused — token via API) |
| `RATE_LIMIT_LOGIN_MAX` | `8` | Login attempts per window |
| `RATE_LIMIT_LOGIN_WINDOW_SECONDS` | `300` | Login rate limit window |
| `RATE_LIMIT_REGISTER_MAX` | `5` | Register attempts per window |
| `RATE_LIMIT_REGISTER_WINDOW_SECONDS` | `3600` | Register rate limit window |

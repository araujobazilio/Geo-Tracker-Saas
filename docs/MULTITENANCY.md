# Multi-tenancy

## Tenant boundary

The tenant boundary is the **Workspace**.

```
User → WorkspaceMembership → Workspace → Projects
```

A `Project` belongs to exactly one `Workspace`. All project-scoped
resources (keywords, competitors, providers, prompts, scans, etc.)
inherit their tenant from their project.

## Workspace types

| Type | Description |
|------|-------------|
| `PERSONAL` | A single user's default workspace |
| `AGENCY` | An agency workspace managing multiple client projects |

Behavior differences are minimal in Phase 1; agency-specific features
(agency dashboard, white-label, team management) are PLANNED for later
phases.

## Roles

| Role | Description |
|------|-------------|
| `OWNER` | Full control, cannot be removed |
| `ADMIN` | Manage members and projects |
| `MEMBER` | Use projects within the workspace |

## Tenant access enforcement (architectural requirement)

Every protected database operation MUST validate that the acting user is
a member of the workspace that owns the target resource.

Never trust `project_id`, `workspace_id`, or `scan_id` provided by the
browser without checking membership. This prevents IDOR vulnerabilities.

### IMPLEMENTED (Phase 1)

- Workspace tenant model (`Workspace`, `WorkspaceMember`).
- Workspace membership model with roles (`OWNER`, `ADMIN`, `MEMBER`).
- Tenant-scoped data model (all project-scoped tables carry `workspace_id`
  directly or via their project).
- `workspace_members (workspace_id, user_id)` unique constraint prevents
  duplicate memberships.
- UUIDs (not sequential IDs) reduce IDOR exposure.

### PLANNED (Phase 2)

- Authenticated tenant-access enforcement (every protected operation
  validates membership against the authenticated user).
- Repository / service authorization layer.
- Role enforcement (OWNER / ADMIN / MEMBER permissions).
- IDOR prevention enforcement.

The data model establishes the structural foundation; the concrete
enforcement layer is implemented in Phase 2 alongside authentication.

## Status

- Workspace tenant model: **IMPLEMENTED** (Phase 1)
- Workspace membership model: **IMPLEMENTED** (Phase 1)
- Tenant-scoped data model: **IMPLEMENTED** (Phase 1)
- Authenticated tenant-access enforcement: **PLANNED** (Phase 2)
- Repository/service authorization: **PLANNED** (Phase 2)
- Role enforcement: **PLANNED** (Phase 2)
- IDOR prevention enforcement: **PLANNED** (Phase 2)
- Agency dashboard / team management: **PLANNED** (Phase 13)

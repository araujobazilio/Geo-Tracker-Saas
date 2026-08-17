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

The concrete enforcement layer (repository / service authorization) is
implemented in Phase 2 alongside authentication. The data model in
Phase 1 establishes the structural foundation:

- `workspace_members (workspace_id, user_id)` unique constraint prevents
  duplicate memberships.
- All project-scoped tables carry `workspace_id` (directly or via their
  project) so tenant filtering is always possible.
- UUIDs (not sequential IDs) reduce IDOR exposure.

## Status

- Data model: **IMPLEMENTED** (Phase 1)
- Membership / role storage: **IMPLEMENTED**
- Authentication + authorization enforcement: **PLANNED** (Phase 2)
- Agency dashboard / team management: **PLANNED** (Phase 13)

# Custom Workspace Design

**Date:** 2026-03-02
**Status:** Approved

## Overview

Introduce **CustomWorkspace** — a user-created, multi-tenant workspace that sits as a peer alongside the existing TenantWorkspace. A CustomWorkspace groups 1+ tenants together, aggregates their knowledge and learnings, and supports its own workspace-specific content. Users can share CustomWorkspaces with others via role-based membership.

## Key Decisions

- **Naming**: `CustomWorkspace` (distinct from `TenantWorkspace`, no inheritance confusion)
- **Relationship**: Peer to TenantWorkspace, not parent/child. No abstract base class — extract later if needed.
- **App rename**: `apps/projects/` → `apps/workspace/` (both workspace types live here)
- **Content reuse**: Existing knowledge/learning models gain an optional FK to CustomWorkspace (dual nullable FK with check constraint). No duplicate tables.
- **Access validation**: Every workspace entry validates the user has TenantMembership for ALL member tenants. Block entirely if any access is missing.
- **Roles**: Owner / Editor / Viewer (full RBAC planned later)
- **Chat**: CustomWorkspace gets its own threads (not merged with tenant threads)
- **Data access**: Sequential per-tenant querying for v1, designed for union schema access later
- **API prefix**: `/api/custom-workspaces/`

## Data Model

### App rename: `apps/projects/` → `apps/workspace/`

TenantWorkspace, TenantSchema, MaterializationRun, TenantMetadata move here. All imports and FKs updated.

### New models (in `apps/workspace/`)

#### CustomWorkspace

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID PK | |
| `name` | CharField | User-chosen name |
| `description` | TextField | Optional |
| `system_prompt` | TextField | Workspace-level agent prompt |
| `created_by` | FK → User | |
| `created_at` | DateTimeField | auto_now_add |
| `updated_at` | DateTimeField | auto_now |

#### CustomWorkspaceTenant (join table)

| Field | Type | Notes |
|-------|------|-------|
| `workspace` | FK → CustomWorkspace | |
| `tenant_workspace` | FK → TenantWorkspace | |
| `added_at` | DateTimeField | auto_now_add |
| `unique_together` | `[workspace, tenant_workspace]` | |

#### WorkspaceMembership

| Field | Type | Notes |
|-------|------|-------|
| `workspace` | FK → CustomWorkspace | |
| `user` | FK → User | |
| `role` | CharField | `owner` / `editor` / `viewer` |
| `invited_by` | FK → User, null | |
| `joined_at` | DateTimeField | auto_now_add |
| `unique_together` | `[workspace, user]` | |

### Modified existing models (in `apps/knowledge/`)

Add optional `custom_workspace = FK(CustomWorkspace, null=True, blank=True)` to:
- **KnowledgeEntry**
- **TableKnowledge**
- **AgentLearning**

Each gets a `CheckConstraint`: exactly one of `workspace` or `custom_workspace` must be non-null (XOR).

### Chat threads

Add optional `custom_workspace = FK(CustomWorkspace, null=True, blank=True)` to the Thread model, same dual-FK pattern.

## API Design

### Endpoints: `/api/custom-workspaces/`

| Endpoint | Method | Role | Purpose |
|----------|--------|------|---------|
| `/` | GET | authenticated | List user's CustomWorkspaces |
| `/` | POST | authenticated | Create (creator = owner) |
| `/<id>/` | GET | member | Workspace details + tenants |
| `/<id>/` | PATCH | owner | Update name, description, system_prompt |
| `/<id>/` | DELETE | owner | Delete workspace |
| `/<id>/enter/` | POST | member | Validate all tenant access, set active |
| `/<id>/tenants/` | GET | member | List member tenants |
| `/<id>/tenants/` | POST | owner | Add tenant |
| `/<id>/tenants/<id>/` | DELETE | owner | Remove tenant |
| `/<id>/members/` | GET | member | List members + roles |
| `/<id>/members/` | POST | owner | Invite (validates invitee tenant access) |
| `/<id>/members/<id>/` | PATCH | owner | Change role |
| `/<id>/members/<id>/` | DELETE | owner | Remove member |

### Access validation (`/enter/`)

Every entry validates:
1. User is a WorkspaceMembership member
2. User has TenantMembership for ALL tenants in the workspace
3. If any tenant access is missing → 403 with list of missing tenants

### Invite validation

Invitee must have TenantMembership for all workspace tenants. Reject otherwise.

### Role permissions

| Action | Owner | Editor | Viewer |
|--------|-------|--------|--------|
| Enter workspace | ✓ | ✓ | ✓ |
| View content | ✓ | ✓ | ✓ |
| Create/edit knowledge & learnings | ✓ | ✓ | ✗ |
| Create chat threads | ✓ | ✓ | ✗ |
| Run recipes | ✓ | ✓ | ✓ |
| Manage tenants | ✓ | ✗ | ✗ |
| Manage members | ✓ | ✗ | ✗ |
| Edit workspace settings | ✓ | ✗ | ✗ |
| Delete workspace | ✓ | ✗ | ✗ |

### Workspace context resolution

Existing APIs (`_resolve_workspace`) extended to check `X-Custom-Workspace` header. If present, resolves CustomWorkspace and re-validates access. Otherwise, falls back to existing TenantWorkspace resolution.

## Frontend Design

### Workspace Selector — Full-Width Tabbed Panel

Replaces the current sidebar dropdown with a full-width modal/panel:

- **Three tab buttons**: Custom | CommCare | Connect (extensible for future providers)
- **Search** scoped to active tab
- **Tab badges** showing count
- **Custom tab**: Shows workspace cards with tenant count and member count
- **CommCare tab**: Lists domains
- **Connect tab**: Lists opportunities with richer metadata (org, program)
- **"+ Create Custom Workspace"** button on Custom tab
- Designed to eventually become a standalone `/workspaces` page

### Content Provenance

In knowledge/learnings list views within a CustomWorkspace:
- Each entry shows a source badge: tenant name (muted) or "This Workspace" (distinct color)
- Editors can only edit workspace-specific entries; tenant entries are read-only
- Filter/group by source available

### Zustand Store

```typescript
interface WorkspaceSlice {
  customWorkspaces: CustomWorkspace[]
  activeCustomWorkspaceId: string | null
  workspaceMode: 'tenant' | 'custom'
  fetchCustomWorkspaces: () => Promise<void>
  enterCustomWorkspace: (id: string) => Promise<void>
  exitCustomWorkspace: () => void
  createCustomWorkspace: (data: CreateWorkspacePayload) => Promise<void>
}
```

`workspaceMode` determines context. When `'custom'`, `X-Custom-Workspace` header sent with all requests.

### Routing

```
/custom-workspaces/:id/settings  → WorkspaceSettingsPage (owner only)

# Existing routes work in both modes
/                                → ChatPanel
/artifacts                       → ArtifactsPage
/knowledge                       → KnowledgePage
/recipes                         → RecipesPage
/data-dictionary                 → DataDictionaryPage
```

### Access Denied UX

When `/enter/` returns 403, show which specific tenants the user lost access to, with guidance to contact workspace owner or tenant admin.

## Agent & Chat Integration

### Context Assembly

When operating in a CustomWorkspace, the agent receives:

1. **System prompts**: Base prompt + workspace prompt + each tenant's prompt
2. **Knowledge**: All entries from member TenantWorkspaces + workspace-specific entries
3. **Learnings**: Same aggregation pattern
4. **Available tenants**: List of all member tenants with their data dictionaries

### Sequential Per-Tenant Querying (v1)

The agent queries each tenant's schema separately via existing MCP tools (which already accept `tenant_id`). Results are combined in the application/response layer. No MCP server changes needed.

### Future: Union Schema Access

Data model supports evolving to simultaneous multi-schema access. The agent would get a single connection with access to all member tenant schemas for cross-tenant JOINs.

## Migration Notes

- App rename `projects` → `workspace` requires updating all imports, URL configs, and `app_label` references
- Existing FKs to TenantWorkspace stay valid (model moves apps but table can keep the same db_table name to avoid data migration)
- New nullable FKs on knowledge/learning models are additive (no data migration, just schema)
- Check constraints added for the XOR validation on dual FKs

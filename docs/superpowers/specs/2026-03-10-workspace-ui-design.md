# Workspace UI Design

**Date:** 2026-03-10
**Status:** Approved

## Overview

Add workspace management UI. The backend already has full CRUD APIs for workspaces, members, and tenants. The frontend currently has a workspace dropdown to switch between workspaces but no way to create, view, or manage them.

## Design Decisions

- Workspace management is accessed via the existing sidebar dropdown (no new nav items)
- Create workspace uses a modal (only 2 fields: name + tenant picker)
- The workspace list lives at `/workspaces`; detail/settings at `/workspaces/:id`
- Detail page uses tabs (Members | Tenants | Settings)
- Role gating: Manager role sees all edit controls; Read/Read-Write sees read-only views

## Components

### 1. Sidebar Dropdown Changes (`Sidebar.tsx`)

Extend the workspace `<Select>` with two items at the bottom, separated by a divider:

- **"Manage workspaces…"** — navigates to `/workspaces`
- **"+ New workspace"** — opens the CreateWorkspaceModal

Use a custom `<SelectItem>` with a visual separator, or replace the `<Select>` with a `<DropdownMenu>` if `<Select>` doesn't support non-value items cleanly.

### 2. CreateWorkspaceModal

Reusable modal triggered from the sidebar dropdown and from the list page.

**Fields:**
- Name (text input, required)
- Data Sources (checkbox list of the user's tenants not yet in a workspace — fetched from existing tenant data)

**Behavior:**
- `POST /api/workspaces/` with `{ name, tenant_ids }`
- On success: close modal, refresh workspace list (`fetchDomains()`), navigate to new workspace detail page
- On error: show inline error message

### 3. WorkspacesPage (`/workspaces`)

List all workspaces the user belongs to.

**Layout:**
- Page header: "Workspaces" title + "+ New workspace" button (opens modal)
- Workspace rows/cards showing: name, role badge (color-coded), member count, tenant count
- Each row is clickable → navigates to `/workspaces/:id`
- Empty state if no workspaces

**Data:** Comes from `domains` in the existing `DomainSlice` (already loaded via `fetchDomains()`).

### 4. WorkspaceDetailPage (`/workspaces/:id`)

Fetches `GET /api/workspaces/:id` for detail (includes `system_prompt`, `schema_status`).

**Header:** Back link to `/workspaces`, workspace name, role badge.

**Tabs:**

#### Members Tab
- Table: avatar/email, display name, role dropdown (Manager only), remove button (Manager only)
- "Add member" button → small inline form or second modal with email field + role select
- `PATCH /api/workspaces/:id/members/:membership_id/` to change role
- `DELETE /api/workspaces/:id/members/:membership_id/` to remove
- Add member: needs a backend endpoint — check if one exists; if not, flag as out of scope for this phase

#### Tenants Tab
- List of tenants linked to this workspace: tenant name, provider
- "Add data source" button (Manager only) → dropdown of user's tenants not already in workspace
- `POST /api/workspaces/:id/tenants/` to add
- `DELETE /api/workspaces/:id/tenants/:wt_id/` to remove
- Read-only if not Manager

#### Settings Tab
- **Rename:** Text input pre-filled with current name + Save button → `PATCH /api/workspaces/:id/`
- **System Prompt:** Textarea with current value + Save button → `PATCH /api/workspaces/:id/`
- **Danger Zone:** "Delete Workspace" button → confirmation dialog → `DELETE /api/workspaces/:id/`, then redirect to `/workspaces`
- Entire tab hidden (or inputs disabled) if not Manager

## State Management

Workspace detail data (members, tenants) is fetched locally within the page components — no new Zustand slices needed. Use `useState` + `useEffect` in the page components, with the `client` fetch wrapper for API calls.

The existing `DomainSlice.fetchDomains()` is called after create/delete to keep the sidebar dropdown in sync.

## Routing

Add to `router.tsx`:
```
/workspaces           → WorkspacesPage
/workspaces/:id       → WorkspaceDetailPage
```

## Role Badge Colors

| Role | Style |
|------|-------|
| manage | green background |
| read_write | blue background |
| read | muted/gray background |

## Out of Scope (This Phase)

- Invite member by email (no `/invite` endpoint exists on backend — members must already be users)
- Workspace schema status display (available/provisioning/unavailable) beyond what detail API returns
- Workspace transfer / ownership changes

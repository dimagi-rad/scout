# Connections & Workspaces List Filtering

**Date:** 2026-04-12
**Status:** Approved
**Scope:** Frontend refactor of ConnectionsPage and WorkspacesPage, plus a small backend change to the workspace list API.

## Problem

With 100+ tenants, the connections page is unusable -- you can't find a specific tenant without scrolling through a flat list of cards. There's no way to search or filter by provider. The workspaces page has the same scaling problem.

## Design Decisions

- **Compact table** for connections (high density, ~20-30 visible rows without scrolling)
- **Toggle buttons** for provider filtering (consistent with KnowledgeList pattern, one-click, always visible)
- **Dialog** for add/edit forms (instead of inline card expansion)
- **Shared SearchFilterBar component** to avoid duplicating search+filter logic across pages
- **Keep workspace rows as clickable cards** (richer content than connections -- member count, source count, role badge)

## Shared Component: SearchFilterBar

New component at `frontend/src/components/SearchFilterBar/SearchFilterBar.tsx`.

### Props

```ts
interface FilterOption {
  value: string
  label: string
  count?: number
}

interface FilterGroup {
  name: string       // key for activeFilters lookup
  options: FilterOption[]
}

interface SearchFilterBarProps {
  search: string
  onSearchChange: (value: string) => void
  placeholder?: string
  filters: FilterGroup[]
  activeFilters: Record<string, string | null>
  onFilterChange: (group: string, value: string | null) => void
}
```

### Rendering

- Left: Search icon + text input (same style as KnowledgeList/ArtifactList)
- Right: One group of toggle buttons per `FilterGroup`. Each group has an implicit "All" button (value `null`) plus one button per option. Active button is filled, rest are outlined. Optional count badge on each button.

## ConnectionsPage Changes

### Layout (top to bottom)

1. Page header: "Connected Accounts" + subtitle (unchanged)
2. Error banner (unchanged)
3. **OAuth Providers section** -- unchanged, stays at top as cards (this list is small)
4. **API Key Domains section:**
   - Section header "API Key Domains" + "Add Domain" button
   - `SearchFilterBar` with:
     - Placeholder: "Search tenants..."
     - One filter group: provider (options derived dynamically from the domain list)
   - Shadcn `<Table>` with columns:
     - **Name** -- `tenant_name || tenant_id`
     - **Provider** -- colored badge (capitalized provider name)
     - **Tenant ID** -- muted text
     - **Actions** -- Edit / Remove text buttons
   - Empty states: "No API key domains connected." or "No tenants match your search."

### Remove Confirmation

Stays inline in the table row -- the normal row content is replaced with confirmation text + Cancel/Confirm Remove buttons. Same pattern as current implementation, adapted to table cells.

### Add/Edit Dialog

Uses the existing shadcn `Dialog` component.

- **Dialog title:** "Add Domain" or "Edit Domain"
- **Form fields:** Same as today (CommCare Domain, Username, API Key)
- **Behavior:** On submit, closes dialog and refreshes the domain list. On cancel, closes dialog.

### Filtering Logic

- **Search:** Case-insensitive substring match against `tenant_name` and `tenant_id`
- **Provider filter:** Exact match on `provider` field
- **Combination:** AND (both must match)

## WorkspacesPage Changes

### Layout (top to bottom)

1. Page header: "Workspaces" + subtitle + "New workspace" button (unchanged)
2. `SearchFilterBar` with:
   - Placeholder: "Search workspaces..."
   - Two filter groups:
     - **Role:** Read / Read+Write / Manage (options derived from workspace list)
     - **Provider:** CommCare / DHIS2 / etc. (options derived from workspace `providers` field)
3. Filtered workspace rows (keep existing `WorkspaceRow` clickable card format, updated to show tenants)
4. Empty/no-results states for filtered views

### WorkspaceRow Updates

The row currently shows member count and a generic "N sources" count. Replace the sources count with the actual tenant list:

- Show each tenant name with a provider badge (same colored badge style as ConnectionsPage table)
- If the workspace has many tenants (e.g. 5+), show the first few and a "+N more" overflow indicator
- Keep member count and role badge as-is

### Search Logic

- Case-insensitive substring match against workspace `name`

### Filter Logic

- **Role:** Exact match on `workspace.role`
- **Provider:** `workspace.tenants.some(t => t.provider === selectedProvider)`
- **Combination:** All active filters AND search combine with AND

## Backend Change

### Workspace List API

Replace `tenant_count` with a `tenants` array in the workspace list serializer response:

```json
{
  "id": "...",
  "name": "My Workspace",
  "role": "manage",
  "member_count": 2,
  "tenants": [
    { "id": "...", "tenant_name": "my-project", "provider": "commcare" },
    { "id": "...", "tenant_name": "nutrition-tracker", "provider": "dhis2" }
  ]
}
```

Each entry in `tenants` is the lightweight shape: `{ id, tenant_name, provider }`. This replaces the `tenant_count` integer -- the count is just `tenants.length` on the frontend.

The `WorkspaceListItem` TypeScript type in `frontend/src/api/workspaces.ts` replaces `tenant_count: number` with `tenants: { id: string; tenant_name: string; provider: string }[]`. Provider filtering uses `workspace.tenants.some(t => t.provider === selectedProvider)`.

## Files Changed

### New

- `frontend/src/components/SearchFilterBar/SearchFilterBar.tsx`

### Modified

- `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx` -- table layout, dialog forms, SearchFilterBar
- `frontend/src/pages/WorkspacesPage/WorkspacesPage.tsx` -- SearchFilterBar, filtering logic, WorkspaceRow updated to show tenant names + provider badges
- `frontend/src/api/workspaces.ts` -- replace `tenant_count` with `tenants` array on `WorkspaceListItem`
- Backend workspace list serializer -- nest tenants with provider info instead of tenant_count

### Unchanged

- OAuth Providers section rendering
- All other API endpoints

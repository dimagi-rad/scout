# Tenant picker: show opportunity IDs + search

## Problem

When adding data sources to a workspace via the **Tenants** tab on `WorkspaceDetailPage`, the available-tenants list shows only the human-readable name plus the provider (e.g. `Mother Baby Wellness [Cohort 1 - Solina]   commcare_connect`). Many CommCare projects share near-identical names ("Mother Baby Wellness [Cohort 1 - Ruwoyd]", "Mother Baby Wellness [Cohort 2 - Ruwoyd]", "Mother Baby Wellness - [Ruwoyd - Learn Experiment 2]", ...), so reproducing a workspace from a list of opportunity IDs (e.g. `524, 675, 874, 938, 1236, 1487, 1488, 1739, 1790`) requires guessing which name maps to which ID.

The opportunity ID is already returned by the API (`UserTenant.tenant_id`, see `frontend/src/api/auth.ts:6`), but never rendered.

## Scope

Smallest viable change. **Out of scope:** a bulk paste-IDs flow, fuzzy matching, debouncing, backend changes.

## Changes

**File:** `frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx`, `TenantsTab` component (lines 293-448).

### 1. Render `tenant_id` in the available list

Available rows currently render `tenant_name` and `provider` inline on one line (lines 374-378). Restructure to match the two-line layout already used by the connected-tenants list below it:

- Line 1 (`font-medium`): `{tenant_name}`
- Line 2 (`text-xs text-muted-foreground`): `#{tenant_id} · {provider}`

### 2. Render `tenant_id` in the connected-tenants list

Lines 403-411 currently show `{tenant_name}` over `{provider}`. Change the subtitle to `#{tenant_id} · {provider}` so the user can audit which opportunities ended up in the workspace.

### 3. Search input above the available list

When the add panel is open (`showAdd && available.length > 0`), render a search input above the rows.

- Single text field; placeholder `Search by name or opportunity ID…`
- Client-side filter: case-insensitive match of the query against `tenant_name` OR `tenant_id` (substring match in both fields)
- `data-testid="tenant-search-input"`
- Empty-result state: `No data sources match "<query>".`
- Search query is reset to `""` whenever the add panel closes (`showAdd` toggles to false)
- Search query is **preserved** when the list refreshes after a successful add — the user is usually adding several in a row that share a substring, and resetting the filter forces them to re-type

## Edge cases

- Query containing only whitespace shows the full list (treat as empty query).
- A user pastes `"#874"` — the leading `#` should not break the match. Strip a leading `#` from the query before comparing.

## Testing

Manual verification via `playwright-cli` against the dev server:

1. `uv run honcho -f Procfile.dev start`
2. Open a workspace's Tenants tab
3. Click **+ Add data source**
4. Confirm each available row shows `#<id> · <provider>`
5. Type `874` → only the matching opportunity remains
6. Type `mother baby` → all MBW opportunities remain
7. Type `#874` → matches
8. Type `zzzz` → empty-state message visible
9. Add one tenant → list refreshes, search query persists
10. Close the add panel → reopen → search input is empty

No new automated tests. The change is presentational filtering over an already-tested data path; logic complexity does not warrant a unit test.

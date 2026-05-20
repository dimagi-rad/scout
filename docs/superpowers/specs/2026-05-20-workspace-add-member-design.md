# Workspace: Add Member — Design

**Status:** Draft, pending implementation
**Date:** 2026-05-20
**Owner:** skelly@dimagi.com

## Problem

Workspace membership management is half-implemented. The backend can list members, change roles, and remove members, but cannot add new ones. The frontend Members tab mirrors that limitation — there is no "Add member" affordance. The only paths that create a `WorkspaceMembership` today are (a) workspace creation (creator → manager) and (b) the `TenantMembership` signal that auto-provisions a personal workspace. There is no way for a manager to bring another existing Scout user into a workspace.

The `WorkspaceMembership.invited_by` field exists on the model but is never written.

## Goals

- Let workspace managers add another existing Scout user to a workspace by email
- Restrict adds to users who already share a tenant with the workspace
- Manager picks the role (Read / Read-Write / Manage) at add time
- Surface clear, actionable errors when the add cannot proceed

## Non-Goals

- Email invite links / pending-invite state
- Sign-up of new users via the add flow
- Bulk add (multiple emails at once)
- Autocomplete / type-ahead member picker
- Cross-tenant adds, even with a superuser override
- Notifying the added user (no email, no in-app notification) — out of scope for v1

## Decisions

1. **Direct add by email, existing users only.** No invite tokens, no email sending, no pending state. If the email does not match a Scout user, the add fails.
2. **Tenant-scoped.** A user can be added only if they have a `TenantMembership` on at least one of the workspace's tenants. No superuser bypass in the API; superusers can still create rows via the Django admin.
3. **Role chosen at add time.** Single request body carries email and role. Default role in the UI is `read_write`.
4. **Distinct error messages for "no such user" vs "not in tenant".** Workspace managers are trusted; clarity beats the marginal privacy gain of a generic error.

## Backend

### Endpoint

Add a `post()` method to the existing `WorkspaceMemberListView` (`apps/workspaces/api/workspace_views.py`). No new view class, no URL changes — `POST /api/workspaces/<workspace_id>/members/` becomes the add endpoint.

**Permissions:** `IsAuthenticated`; caller must have `WorkspaceRole.MANAGE` on the workspace, same gate used by `WorkspaceMemberDetailView.patch/delete`.

### Request

```json
{ "email": "alice@example.com", "role": "read_write" }
```

### Validation (in order; first failure short-circuits)

| Check | Failure response |
|---|---|
| `email` present and well-formed | `400 {"error": "Email is required"}` |
| `role` in `WorkspaceRole.values` | `400 {"error": "Invalid role"}` |
| User exists (`User.objects.filter(email__iexact=email).first()`) | `404 {"error": "No Scout user with that email"}` |
| User has a `TenantMembership` on at least one workspace tenant | `403 {"error": "User is not part of this workspace's tenants"}` |
| No existing `WorkspaceMembership` for (workspace, user) | `409 {"error": "User is already a member"}` |

The `unique_together = [["workspace", "user"]]` constraint on `WorkspaceMembership` is the ultimate guard against duplicate-add races, but the explicit pre-check produces a clean error instead of an `IntegrityError`.

### On success

Create the row with `invited_by=request.user` and return `201` with the same shape `GET /members/` returns for a single member:

```json
{
  "id": "...",
  "user_id": "...",
  "email": "...",
  "name": "...",
  "role": "read_write",
  "created_at": "..."
}
```

Identical shape to `GET` so the frontend can append the result to its local `members` array without a refetch.

### Tenant-scope query

```python
workspace_tenant_ids = workspace.workspace_tenants.values_list("tenant_id", flat=True)
shares_tenant = TenantMembership.objects.filter(
    user=target, tenant_id__in=workspace_tenant_ids
).exists()
```

### `invited_by` semantics

This is the first writer of `WorkspaceMembership.invited_by`. The signal-driven auto-creation paths and workspace-creation path continue to leave it null (those memberships have no inviter). Only the new POST endpoint populates it.

## Frontend

### Component changes

All changes are inside `pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx`, in the existing `MembersTab` component. No new files.

Add a button row above the members table, visible only when `isManager` is true (the prop already exists):

```
[ Members tab header — "3 members" ]
[ + Add member ]                                    ← collapsed state
   ↓ click
[ email input ] [ role select ] [Add] [Cancel]      ← expanded state
[ error message slot, only if error ]
[ table ... ]
```

### Interaction model

- Inline form, not a modal. Only two inputs; a modal is overkill.
- Click "Add member" → button replaced by inline form, focus auto-moves to email input
- Submit → `workspaceApi.addMember(workspaceId, { email, role })`. On `201`, append the returned member to local `members`, collapse the form, clear inputs.
- Cancel → collapse, clear inputs, clear error
- Error → render verbatim backend message under the form; no retry button (user edits the email and resubmits)
- Submitting → both inputs and the Add button disabled, label shows "Adding…"

### API client

Add to `frontend/src/api/workspaces.ts`:

```ts
addMember: (
  workspaceId: string,
  body: { email: string; role: WorkspaceMember["role"] }
) =>
  api.post<WorkspaceMember>(`/api/workspaces/${workspaceId}/members/`, body),
```

### Role select

- Options: Read / Read-Write / Manage (same three as the existing per-row role-change dropdown)
- Default selection: `read_write`

### data-testid attributes (per CLAUDE.md convention)

- `add-member-button` — trigger button
- `add-member-email` — email input
- `add-member-role` — role select trigger
- `add-member-submit` — submit button
- `add-member-cancel` — cancel button
- `add-member-error` — error message slot

### What this UI does NOT do

- No email autocomplete from tenant membership
- No multi-email / bulk add
- No success toast — the new row appearing in the table is sufficient feedback

## Edge cases

| Case | Handling |
|---|---|
| Email casing differences | Case-insensitive lookup (`email__iexact`); stored email is not normalized |
| User exists but has no tenant membership at all | Falls through to shared-tenants check → 403 |
| Workspace with zero tenants (rare; only via direct row deletion) | Shared-tenants check always fails → 403. Broken workspaces should not be addable into. |
| Manager tries to add themselves | Duplicate check → 409 |
| Two managers add same email concurrently | First wins (201); second hits duplicate check → 409. `unique_together` is the ultimate guard. |
| Caller loses MANAGE role mid-request | Fresh role check via `resolve_workspace` → 403 |

## Testing

### Backend (`tests/test_workspace_members.py` or equivalent location of existing member tests)

Extend the existing `WorkspaceMemberListView` test class with a new section covering POST:

- `201` — manager adds a valid same-tenant user, once for each of the three roles; `invited_by` is populated with the caller
- `400` — missing email; malformed email; invalid role value
- `403` — non-manager caller; manager adds a user with no shared tenant
- `404` — email matches no user
- `409` — target is already a member; case-insensitive duplicate (e.g. `ALICE@X.COM` when `alice@x.com` is already a member)
- Response body shape matches `GET /members/` list-item shape (so the frontend's append-without-refetch optimization is safe)

### Frontend (extend the existing `WorkspaceDetailPage` test file)

- "Add member" button only visible when `isManager` is true
- Submitting calls `addMember` and appends the returned member to the table on 201
- Each backend error message is rendered in `add-member-error` verbatim
- Cancel collapses the form and clears email/role/error state

### Out of scope for testing

- No e2e/QA scenario. The existing `tests/qa/` suite for workspaces does not exercise member management; backend coverage on the wire contract plus frontend coverage on the form is sufficient for v1.

## Open questions

None outstanding at design time. Items resolved during brainstorming:

- Add flow shape → direct add, existing users only
- Tenant scope → restrict to shared tenants, no superuser override at the API
- Role selection → picked at add time, default `read_write`
- Error message granularity → distinct messages (privacy not a concern for manager-only flow)

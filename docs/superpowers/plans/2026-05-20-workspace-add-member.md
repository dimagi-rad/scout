# Workspace Add-Member Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let workspace managers add an existing Scout user to a workspace by email, restricted to users who share a tenant with the workspace, with the role chosen at add time.

**Architecture:** Add a `POST` handler to the existing `WorkspaceMemberListView` (no new view class, no new URL). Validation runs in a fixed order: email format → role value → user exists → user shares a tenant → not already a member. Frontend extends the existing `MembersTab` with an inline collapsible form above the table.

**Tech Stack:** Django 5 (sync DRF `APIView`, since `WorkspaceMemberListView` is already sync), pytest with `tests/conftest.py` fixtures, React 19 with the existing shadcn/ui `Select`/`Button` primitives.

**Note on frontend tests:** The Scout frontend has Playwright e2e for the widget only — no unit/component test framework wired up for app pages. Coverage for the UI form is therefore manual verification in the browser (Task 6). Backend tests cover the full wire contract.

**Spec:** `docs/superpowers/specs/2026-05-20-workspace-add-member-design.md`

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `apps/workspaces/api/workspace_views.py` | Modify (`WorkspaceMemberListView`) | Add `post()` method |
| `tests/test_workspace_management.py` | Modify (append `TestMemberAdd` class) | Backend test coverage |
| `frontend/src/api/workspaces.ts` | Modify (extend `workspaceApi`) | Add `addMember` client method |
| `frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx` | Modify (`MembersTab` component) | Inline add-member form UI |

No new files. The work fits naturally into existing files without growing any of them past a reasonable size.

---

## Task 1: Backend — POST endpoint happy path (TDD)

**Files:**
- Modify: `apps/workspaces/api/workspace_views.py` (add `post()` to `WorkspaceMemberListView`, currently lines 255–279)
- Modify: `tests/test_workspace_management.py` (append a new `TestMemberAdd` class at end of file)

### Step 1.1: Write the failing happy-path test

Append to `tests/test_workspace_management.py`:

```python
# ---------------------------------------------------------------------------
# Member management: add member
# ---------------------------------------------------------------------------


class TestMemberAdd:
    def test_manager_can_add_same_tenant_user(self, client, user, workspace, tenant, db):
        """Manager adds an existing user who shares the workspace's tenant."""
        from apps.users.models import TenantMembership

        target = User.objects.create_user(email="alice@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "alice@example.com", "role": WorkspaceRole.READ_WRITE},
            content_type="application/json",
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["email"] == "alice@example.com"
        assert body["role"] == WorkspaceRole.READ_WRITE
        assert "id" in body and "user_id" in body and "name" in body and "created_at" in body

        membership = WorkspaceMembership.objects.get(workspace=workspace, user=target)
        assert membership.invited_by == user
        assert membership.role == WorkspaceRole.READ_WRITE
```

- [ ] **Step 1.2: Run the test, confirm it fails**

```bash
uv run pytest tests/test_workspace_management.py::TestMemberAdd::test_manager_can_add_same_tenant_user -v
```

Expected: FAIL with `405 Method Not Allowed` (no `post()` on the view yet).

- [ ] **Step 1.3: Implement the POST handler**

In `apps/workspaces/api/workspace_views.py`, modify `WorkspaceMemberListView` (the class that currently has only a `get()` method). Replace the docstring and add `post()`:

```python
class WorkspaceMemberListView(APIView):
    """
    GET  /api/workspaces/<workspace_id>/members/  — list members (any member).
    POST /api/workspaces/<workspace_id>/members/  — add an existing user as a member (manage only).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        # unchanged — leave existing implementation in place
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        memberships = WorkspaceMembership.objects.filter(workspace=workspace).select_related("user")
        results = [
            {
                "id": str(m.id),
                "user_id": str(m.user.id),
                "email": m.user.email,
                "name": m.user.get_full_name(),
                "role": m.role,
                "created_at": m.created_at.isoformat(),
            }
            for m in memberships
        ]
        return Response(results)

    def post(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only managers can add members."},
                status=status.HTTP_403_FORBIDDEN,
            )

        email = (request.data.get("email") or "").strip()
        if not email or "@" not in email:
            return Response(
                {"error": "Email is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        role = request.data.get("role")
        if role not in WorkspaceRole.values:
            return Response(
                {"error": "Invalid role"}, status=status.HTTP_400_BAD_REQUEST
            )

        from django.contrib.auth import get_user_model

        target = get_user_model().objects.filter(email__iexact=email).first()
        if target is None:
            return Response(
                {"error": "No Scout user with that email"},
                status=status.HTTP_404_NOT_FOUND,
            )

        workspace_tenant_ids = workspace.workspace_tenants.values_list("tenant_id", flat=True)
        shares_tenant = TenantMembership.objects.filter(
            user=target, tenant_id__in=workspace_tenant_ids
        ).exists()
        if not shares_tenant:
            return Response(
                {"error": "User is not part of this workspace's tenants"},
                status=status.HTTP_403_FORBIDDEN,
            )

        if WorkspaceMembership.objects.filter(workspace=workspace, user=target).exists():
            return Response(
                {"error": "User is already a member"},
                status=status.HTTP_409_CONFLICT,
            )

        new_membership = WorkspaceMembership.objects.create(
            workspace=workspace,
            user=target,
            role=role,
            invited_by=request.user,
        )
        return Response(
            {
                "id": str(new_membership.id),
                "user_id": str(target.id),
                "email": target.email,
                "name": target.get_full_name(),
                "role": new_membership.role,
                "created_at": new_membership.created_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )
```

Note: `from django.contrib.auth import get_user_model` goes at the module top of `workspace_views.py`, not inside `post()`. Per project code style (`CLAUDE.md`): imports at module level. Move it up with the existing imports.

Final top-of-file imports should include:

```python
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Count
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chat.models import Thread
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace
```

And then `get_user_model()` is called from the function body. (The `User` symbol is not bound at module load.)

- [ ] **Step 1.4: Run the happy-path test, confirm it passes**

```bash
uv run pytest tests/test_workspace_management.py::TestMemberAdd::test_manager_can_add_same_tenant_user -v
```

Expected: PASS.

- [ ] **Step 1.5: Commit**

```bash
git add apps/workspaces/api/workspace_views.py tests/test_workspace_management.py
git commit -m "feat(workspaces): add POST /members/ endpoint to add existing user"
```

---

## Task 2: Backend — error cases (TDD)

**Files:**
- Modify: `tests/test_workspace_management.py` (extend `TestMemberAdd`)

These tests should all pass against the implementation from Task 1. We are not modifying `post()` further; we are filling in coverage.

- [ ] **Step 2.1: Add tests for 400/403/404/409 cases**

Append these methods inside `TestMemberAdd` (below `test_manager_can_add_same_tenant_user`):

```python
    def test_missing_email_returns_400(self, client, user, workspace):
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "email" in resp.json()["error"].lower()

    def test_malformed_email_returns_400(self, client, user, workspace):
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "not-an-email", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_role_returns_400(self, client, user, workspace, tenant, db):
        from apps.users.models import TenantMembership

        target = User.objects.create_user(email="alice@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "alice@example.com", "role": "admin"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "role" in resp.json()["error"].lower()

    def test_unknown_email_returns_404(self, client, user, workspace):
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "ghost@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 404
        assert "no scout user" in resp.json()["error"].lower()

    def test_user_without_shared_tenant_returns_403(self, client, user, workspace, db):
        """Target user exists but has no TenantMembership on this workspace's tenants."""
        outsider = User.objects.create_user(email="outsider@example.com", password="pass")
        # Deliberately no TenantMembership

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "outsider@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 403
        assert "tenant" in resp.json()["error"].lower()

    def test_non_manager_cannot_add_members(self, client, workspace, tenant, db):
        from apps.users.models import TenantMembership

        writer = User.objects.create_user(email="wr@example.com", password="pass")
        TenantMembership.objects.create(user=writer, tenant=tenant)
        WorkspaceMembership.objects.create(
            workspace=workspace, user=writer, role=WorkspaceRole.READ_WRITE
        )
        target = User.objects.create_user(email="alice@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)

        client.force_login(writer)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "alice@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 403
        assert "manager" in resp.json()["error"].lower()

    def test_existing_member_returns_409(self, client, user, workspace, tenant, db):
        from apps.users.models import TenantMembership

        target = User.objects.create_user(email="alice@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)
        WorkspaceMembership.objects.create(
            workspace=workspace, user=target, role=WorkspaceRole.READ
        )

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "alice@example.com", "role": WorkspaceRole.READ_WRITE},
            content_type="application/json",
        )
        assert resp.status_code == 409
        assert "already" in resp.json()["error"].lower()

    def test_case_insensitive_duplicate_returns_409(self, client, user, workspace, tenant, db):
        """Adding ALICE@X.COM when alice@x.com is already a member should 409."""
        from apps.users.models import TenantMembership

        target = User.objects.create_user(email="alice@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)
        WorkspaceMembership.objects.create(
            workspace=workspace, user=target, role=WorkspaceRole.READ
        )

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "ALICE@EXAMPLE.COM", "role": WorkspaceRole.READ_WRITE},
            content_type="application/json",
        )
        assert resp.status_code == 409

    def test_add_with_role_read(self, client, user, workspace, tenant, db):
        from apps.users.models import TenantMembership

        target = User.objects.create_user(email="r@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "r@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 201
        assert resp.json()["role"] == WorkspaceRole.READ

    def test_add_with_role_manage(self, client, user, workspace, tenant, db):
        from apps.users.models import TenantMembership

        target = User.objects.create_user(email="m@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "m@example.com", "role": WorkspaceRole.MANAGE},
            content_type="application/json",
        )
        assert resp.status_code == 201
        assert resp.json()["role"] == WorkspaceRole.MANAGE
```

- [ ] **Step 2.2: Run the full `TestMemberAdd` class**

```bash
uv run pytest tests/test_workspace_management.py::TestMemberAdd -v
```

Expected: all 10 tests pass.

- [ ] **Step 2.3: Run the full workspace management suite to confirm nothing regressed**

```bash
uv run pytest tests/test_workspace_management.py -v
```

Expected: all tests pass (the existing classes + the new `TestMemberAdd`).

- [ ] **Step 2.4: Run ruff**

```bash
uv run ruff check apps/workspaces/api/workspace_views.py tests/test_workspace_management.py
uv run ruff format apps/workspaces/api/workspace_views.py tests/test_workspace_management.py
```

Expected: clean.

- [ ] **Step 2.5: Commit**

```bash
git add tests/test_workspace_management.py
git commit -m "test(workspaces): cover validation paths for POST /members/"
```

---

## Task 3: Frontend — API client method

**Files:**
- Modify: `frontend/src/api/workspaces.ts` (add `addMember` inside the `workspaceApi` object, in the `// ── Members ──` section)

- [ ] **Step 3.1: Add the `addMember` method**

In `frontend/src/api/workspaces.ts`, find the `// ── Members ──` block (currently containing `getMembers`, `updateMember`, `removeMember`). Insert `addMember` right after `getMembers`:

```ts
  // ── Members ──────────────────────────────────────────────────────────────

  getMembers: (workspaceId: string) =>
    api.get<WorkspaceMember[]>(`/api/workspaces/${workspaceId}/members/`),

  addMember: (
    workspaceId: string,
    body: { email: string; role: WorkspaceMember["role"] },
  ) =>
    api.post<WorkspaceMember>(
      `/api/workspaces/${workspaceId}/members/`,
      body,
    ),

  updateMember: (workspaceId: string, membershipId: string, role: WorkspaceMember["role"]) =>
    api.patch<{ id: string; role: string }>(
      `/api/workspaces/${workspaceId}/members/${membershipId}/`,
      { role },
    ),

  removeMember: (workspaceId: string, membershipId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/members/${membershipId}/`),
```

The return type is `WorkspaceMember` (full record) — this matches the backend's 201 response shape, so on success the consumer can `setMembers((prev) => [...prev, result])` with no refetch.

- [ ] **Step 3.2: Run the type check**

```bash
cd frontend && bunx tsc --noEmit
```

Expected: clean.

- [ ] **Step 3.3: Run lint**

```bash
cd frontend && bun run lint
```

Expected: clean.

- [ ] **Step 3.4: Commit**

```bash
git add frontend/src/api/workspaces.ts
git commit -m "feat(frontend): add workspaceApi.addMember client method"
```

---

## Task 4: Frontend — inline add-member form

**Files:**
- Modify: `frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx` — extend the `MembersTab` component (currently lines 22–160)

- [ ] **Step 4.1: Extend the `MembersTab` component**

Apply three changes inside the `MembersTab` component:

**Change 1 — add state hooks** (insert directly below the existing `useState` calls, after `setMutationError`):

```tsx
  // Add-member form state
  const [addOpen, setAddOpen] = useState(false)
  const [addEmail, setAddEmail] = useState("")
  const [addRole, setAddRole] = useState<WorkspaceMember["role"]>("read_write")
  const [addSubmitting, setAddSubmitting] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
```

**Change 2 — add the submit handler** (insert below `handleRemove`):

```tsx
  async function handleAdd() {
    const email = addEmail.trim()
    if (!email) {
      setAddError("Email is required")
      return
    }
    setAddSubmitting(true)
    setAddError(null)
    try {
      const newMember = await workspaceApi.addMember(workspaceId, {
        email,
        role: addRole,
      })
      setMembers((prev) => [...prev, newMember])
      setAddEmail("")
      setAddRole("read_write")
      setAddOpen(false)
    } catch (err) {
      setAddError(err instanceof ApiError ? err.message : "Failed to add member")
    } finally {
      setAddSubmitting(false)
    }
  }

  function handleAddCancel() {
    setAddOpen(false)
    setAddEmail("")
    setAddRole("read_write")
    setAddError(null)
  }
```

**Change 3 — render the form** between the member-count header and the table. Replace the existing block:

```tsx
      <div className="mb-4 flex items-center justify-between">
        <span className="text-sm text-muted-foreground">
          {members.length} {members.length === 1 ? "member" : "members"}
        </span>
      </div>
      {mutationError && (
        <p className="mb-3 text-sm text-destructive">{mutationError}</p>
      )}
```

With:

```tsx
      <div className="mb-4 flex items-center justify-between">
        <span className="text-sm text-muted-foreground">
          {members.length} {members.length === 1 ? "member" : "members"}
        </span>
        {isManager && !addOpen && (
          <Button
            size="sm"
            onClick={() => setAddOpen(true)}
            data-testid="add-member-button"
          >
            + Add member
          </Button>
        )}
      </div>

      {isManager && addOpen && (
        <div className="mb-4 rounded-lg border p-3" data-testid="add-member-form">
          <div className="flex items-center gap-2">
            <input
              type="email"
              autoFocus
              placeholder="alice@example.com"
              className="h-9 flex-1 rounded-md border bg-background px-3 text-sm"
              value={addEmail}
              onChange={(e) => setAddEmail(e.target.value)}
              disabled={addSubmitting}
              data-testid="add-member-email"
            />
            <Select
              value={addRole}
              onValueChange={(v) => setAddRole(v as WorkspaceMember["role"])}
              disabled={addSubmitting}
            >
              <SelectTrigger className="h-9 w-36" data-testid="add-member-role">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="read">Read</SelectItem>
                <SelectItem value="read_write">Read-Write</SelectItem>
                <SelectItem value="manage">Manager</SelectItem>
              </SelectContent>
            </Select>
            <Button
              size="sm"
              onClick={handleAdd}
              disabled={addSubmitting}
              data-testid="add-member-submit"
            >
              {addSubmitting ? "Adding…" : "Add"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={handleAddCancel}
              disabled={addSubmitting}
              data-testid="add-member-cancel"
            >
              Cancel
            </Button>
          </div>
          {addError && (
            <p className="mt-2 text-sm text-destructive" data-testid="add-member-error">
              {addError}
            </p>
          )}
        </div>
      )}

      {mutationError && (
        <p className="mb-3 text-sm text-destructive">{mutationError}</p>
      )}
```

Notes:
- The plain `<input>` matches the styling pattern used elsewhere in the codebase for simple text inputs (e.g. the workspace-rename input in the same file). If a shared `Input` component exists and is preferred, swap it in — but plain styled `<input>` is acceptable per the existing patterns in this file.
- `autoFocus` puts the cursor in the email field as soon as the form opens (spec requirement: focus auto-moves to email input).
- The "+ Add member" button is hidden while the form is open, so the button doesn't collide visually with the inline form.

- [ ] **Step 4.2: Type check**

```bash
cd frontend && bunx tsc --noEmit
```

Expected: clean.

- [ ] **Step 4.3: Lint**

```bash
cd frontend && bun run lint
```

Expected: clean.

- [ ] **Step 4.4: Commit**

```bash
git add frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx
git commit -m "feat(frontend): add inline add-member form to Members tab"
```

---

## Task 5: Run the full backend test suite

- [ ] **Step 5.1: Run the full pytest suite**

```bash
uv run pytest
```

Expected: all tests pass. If any test outside `tests/test_workspace_management.py` fails because it asserts the old `POST /members/` returns `405`, update that assertion to match the new behavior.

If the suite is clean, no commit needed — this is verification only.

---

## Task 6: Manual verification in the browser

The frontend has no unit/component tests for app pages (only Playwright e2e for the widget), so we verify the UI by hand.

- [ ] **Step 6.1: Start dependencies and dev servers**

In one terminal:

```bash
docker compose up platform-db mcp-server
```

In another (from the repo root):

```bash
uv run honcho -f Procfile.dev start
```

This starts Django on `:8000`, the Vite dev server on `:5173`, and the MCP server on `:8100`.

- [ ] **Step 6.2: Seed two test users that share a tenant**

```bash
uv run python manage.py shell -c "
from django.contrib.auth import get_user_model
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import Workspace, WorkspaceTenant, WorkspaceMembership, WorkspaceRole

User = get_user_model()
mgr, _ = User.objects.get_or_create(email='mgr@example.com', defaults={'is_active': True})
mgr.set_password('pass'); mgr.save()

target, _ = User.objects.get_or_create(email='target@example.com', defaults={'is_active': True})
target.set_password('pass'); target.save()

tenant, _ = Tenant.objects.get_or_create(
    provider='commcare', external_id='manual-test', defaults={'canonical_name': 'Manual Test'}
)
TenantMembership.objects.get_or_create(user=mgr, tenant=tenant)
TenantMembership.objects.get_or_create(user=target, tenant=tenant)

ws, _ = Workspace.objects.get_or_create(name='Manual Test WS', created_by=mgr)
WorkspaceTenant.objects.get_or_create(workspace=ws, tenant=tenant)
WorkspaceMembership.objects.get_or_create(workspace=ws, user=mgr, defaults={'role': WorkspaceRole.MANAGE})
print('Workspace:', ws.id)
"
```

- [ ] **Step 6.3: Verify the happy path**

1. Open `http://localhost:5173`, log in as `mgr@example.com` / `pass`.
2. Navigate to the workspace detail page → Members tab.
3. Confirm the **+ Add member** button is visible.
4. Click it. Confirm:
   - The button disappears
   - An inline form appears with the email input focused, role defaulting to "Read-Write", and Add / Cancel buttons.
5. Type `target@example.com`, leave role on Read-Write, click **Add**.
6. Confirm:
   - The form collapses
   - The "+ Add member" button reappears
   - A new row for `target@example.com` (Read-Write) appears in the table
   - The member count goes from "1 member" to "2 members"

- [ ] **Step 6.4: Verify error states**

For each case below, open the form fresh (refresh the page if needed) and confirm the error message renders under the form in red, and the form stays open so you can edit and retry:

1. **Unknown user** — type `ghost@example.com` → click Add → expect "No Scout user with that email"
2. **Outsider** — create a user with no TenantMembership on this workspace's tenant via shell, then try their email → expect "User is not part of this workspace's tenants"
3. **Already a member** — type `target@example.com` again (after Step 6.3) → expect "User is already a member"
4. **Empty email** — leave the field empty and click Add → expect "Email is required"

Snippet for the "outsider" user:

```bash
uv run python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
u, _ = User.objects.get_or_create(email='outsider@example.com', defaults={'is_active': True})
u.set_password('pass'); u.save()
"
```

- [ ] **Step 6.5: Verify the button is hidden for non-managers**

1. Log out, log in as `target@example.com` / `pass`.
2. Open the same workspace's Members tab.
3. Confirm **no "+ Add member" button** is visible.
4. Confirm the role dropdown is replaced with a `RoleBadge` (read-only display, per the existing `isManager` logic).

- [ ] **Step 6.6: Stop the dev servers**

`Ctrl+C` in the honcho terminal and `docker compose down` in the other.

No commit for this task — verification only. If you find a bug, file it as a follow-up rather than fixing it in this plan.

---

## Done

After Task 6:

- Backend: `POST /api/workspaces/<id>/members/` with full validation and 10 tests passing
- Frontend: inline add-member form, type-checked and lint-clean
- Manually verified happy path, error states, and non-manager view in the browser

The `WorkspaceMembership.invited_by` field, which existed unused on the model, is now populated by this endpoint — closing a model gap noted during the brainstorm.

# Workspace UI Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add workspace management UI — a dropdown-driven entry point, a list page, a create modal, and a tabbed detail page with Members, Tenants, and Settings tabs.

**Architecture:** The existing Sidebar workspace `<Select>` becomes a `<DropdownMenu>` with two new actions at the bottom. A new `WorkspacesPage` at `/workspaces` lists all workspaces. A `WorkspaceDetailPage` at `/workspaces/:id` uses Radix UI Tabs for Members / Tenants / Settings. Local component state handles all workspace-detail data; the global `DomainSlice` is refreshed after create/delete to keep the sidebar in sync.

**Tech Stack:** React 19, TypeScript, Tailwind CSS 4, Radix UI (`radix-ui` unified package), Lucide icons, `api` client wrapper (`frontend/src/api/client.ts`), React Router v6.

---

## Chunk 1: Backend additions + frontend foundation

Three small backend changes are needed. We also add the Tabs UI component and workspace API module before touching any pages.

### Task 1: Allow creating workspaces with no initial tenants

**Why:** The create modal has a name-only field (tenants are added after creation via the Tenants tab). The current backend rejects `tenant_ids: []` with a 400 error.

**Files:**
- Modify: `apps/projects/api/workspace_views.py` (`WorkspaceListView.post`)
- Modify: `tests/test_workspace_management.py` (add test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workspace_management.py` inside the existing `TestWorkspaceCreate` class (following the exact pattern of other methods in that class — uses `client` Django test Client fixture with `content_type="application/json"`, NOT `api_client`):

```python
def test_create_workspace_with_no_tenants(self, client, user):
    """POST /api/workspaces/ succeeds with tenant_ids=[] (tenants added later)."""
    client.force_login(user)
    resp = client.post(
        "/api/workspaces/",
        {"name": "Empty WS", "tenant_ids": []},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.json()
    assert resp.json()["name"] == "Empty WS"
    assert resp.json()["tenant_count"] == 0
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
uv run pytest "tests/test_workspace_management.py::TestWorkspaceCreate::test_create_workspace_with_no_tenants" -v
```

Expected: FAIL — `"At least one tenant_id is required."` 400 error.

- [ ] **Step 3: Remove the tenant_ids non-empty check in WorkspaceListView.post**

In `apps/projects/api/workspace_views.py`, remove these lines from `WorkspaceListView.post`:

```python
        if not tenant_ids:
            return Response(
                {"error": "At least one tenant_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
```

The surrounding code (name check above, accessible_tenant_ids validation below) stays intact. The validation loop still runs — it just runs zero iterations when `tenant_ids` is empty, which is correct.

- [ ] **Step 4: Run the test to confirm it passes**

```bash
uv run pytest "tests/test_workspace_management.py::TestWorkspaceCreate::test_create_workspace_with_no_tenants" -v
```

- [ ] **Step 5: Run full suite**

```bash
uv run pytest --tb=short -q
```

Expected: all tests pass (including existing tests that create workspaces with tenants).

- [ ] **Step 6: Commit**

```bash
git add apps/projects/api/workspace_views.py tests/test_workspace_management.py
git commit -m "feat: allow creating workspaces with no initial tenants"
```

---

### Task 2: Add GET to WorkspaceTenantView

**Why:** The Tenants tab needs to list the current workspace's tenants. No GET exists on this view.

**Files:**
- Modify: `apps/projects/api/workspace_views.py` (WorkspaceTenantView class)
- Modify: `tests/test_workspace_tenant_api.py` (add test — uses `api_client` and `workspace` fixtures already defined in that file)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workspace_tenant_api.py` as a standalone function (following the existing pattern in that file — standalone functions using `api_client`, `user`, `workspace` fixtures):

```python
def test_list_workspace_tenants(api_client, user, workspace):
    """GET /api/workspaces/<id>/tenants/ returns current tenants."""
    api_client.force_login(user)
    response = api_client.get(f"/api/workspaces/{workspace.id}/tenants/")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    # workspace fixture has one tenant (defined in this file's fixtures)
    assert len(data) == 1
    assert "id" in data[0]           # WorkspaceTenant UUID
    assert "tenant_id" in data[0]    # internal Tenant UUID
    assert "tenant_name" in data[0]
    assert "provider" in data[0]
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
uv run pytest tests/test_workspace_tenant_api.py::test_list_workspace_tenants -v
```

Expected: FAIL — `WorkspaceTenantView` has no GET method, returns 405.

- [ ] **Step 3: Add GET method to WorkspaceTenantView**

In `apps/projects/api/workspace_views.py`, add a `get` method to `WorkspaceTenantView` (before `post`):

```python
def get(self, request, workspace_id):
    workspace, membership, err = resolve_workspace(request, workspace_id)
    if err:
        return err

    tenants = []
    for wt in WorkspaceTenant.objects.filter(workspace=workspace).select_related("tenant"):
        tenants.append({
            "id": str(wt.id),
            "tenant_id": str(wt.tenant.id),
            "tenant_name": wt.tenant.canonical_name,
            "provider": wt.tenant.provider,
        })
    return Response(tenants)
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
uv run pytest tests/test_workspace_tenant_api.py::test_list_workspace_tenants -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite to check for regressions**

```bash
uv run pytest --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/projects/api/workspace_views.py tests/test_workspace_tenant_api.py
git commit -m "feat: add GET /api/workspaces/<id>/tenants/ to list workspace tenants"
```

---

### Task 3: Expose internal tenant UUID in GET /api/auth/tenants/

**Why:** The Tenants tab "Add data source" picker needs to post a `tenant_id` (internal Tenant UUID) to the backend. The current response only returns TenantMembership ID and external ID.

**Files:**
- Modify: `apps/users/views.py` (tenant_list_view response)
- Modify: `tests/test_tenant_api.py` (add test to existing `TestTenantCredentialDeleteAPI` class or as a new standalone class — follow the file's pattern using the `user` pytest fixture and `client` pytest-django fixture)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tenant_api.py` as a new `@pytest.mark.django_db` standalone function (not inside the existing class):

```python
@pytest.mark.django_db
def test_tenant_list_includes_uuid(user, client):
    """GET /api/auth/tenants/ response includes internal tenant UUID."""
    tm = _make_membership(user, external_id="uuid-test", canonical_name="UUID Test")
    client.force_login(user)
    response = client.get("/api/auth/tenants/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    entry = next(e for e in data if e["tenant_id"] == "uuid-test")
    assert "tenant_uuid" in entry
    assert entry["tenant_uuid"] == str(tm.tenant.id)
```

Note: `_make_membership` is a helper already defined at the top of `test_tenant_api.py`.

- [ ] **Step 2: Run the test to confirm it fails**

```bash
uv run pytest tests/test_tenant_api.py::test_tenant_list_includes_uuid -v
```

Expected: FAIL — `tenant_uuid` key absent.

- [ ] **Step 3: Add `tenant_uuid` to tenant_list_view**

In `apps/users/views.py`, in the `tenant_list_view` function, update the `memberships.append(...)` dict (the async for loop around line 100):

```python
memberships.append(
    {
        "id": str(tm.id),
        "provider": tm.tenant.provider,
        "tenant_id": tm.tenant.external_id,
        "tenant_uuid": str(tm.tenant.id),   # ← add this line
        "tenant_name": tm.tenant.canonical_name,
        "last_selected_at": (
            tm.last_selected_at.isoformat() if tm.last_selected_at else None
        ),
    }
)
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
uv run pytest -k "test_tenant_list_includes_uuid" -v
```

- [ ] **Step 5: Run full suite**

```bash
uv run pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add apps/users/views.py tests/test_tenant_api.py
git commit -m "feat: add tenant_uuid field to GET /api/auth/tenants/ response"
```

---

### Task 4: Create Tabs UI component

**Why:** No Tabs component exists. WorkspaceDetailPage needs it. All other UI components follow the same Radix UI wrapper pattern.

**Files:**
- Create: `frontend/src/components/ui/tabs.tsx`

- [ ] **Step 1: Create the Tabs component**

Following the exact pattern of `frontend/src/components/ui/dialog.tsx` (imports from `radix-ui`, wraps primitives, uses `cn`):

```typescript
import * as React from "react"
import { Tabs as TabsPrimitive } from "radix-ui"
import { cn } from "@/lib/utils"

function Tabs({ ...props }: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return <TabsPrimitive.Root data-slot="tabs" {...props} />
}

function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      className={cn(
        "inline-flex h-9 items-center justify-start rounded-lg border-b border-border w-full gap-0 bg-transparent p-0",
        className,
      )}
      {...props}
    />
  )
}

function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        "inline-flex items-center justify-center whitespace-nowrap px-4 py-2 text-sm font-medium transition-all",
        "border-b-2 border-transparent -mb-px",
        "text-muted-foreground hover:text-foreground",
        "data-[state=active]:border-primary data-[state=active]:text-foreground",
        "disabled:pointer-events-none disabled:opacity-50",
        className,
      )}
      {...props}
    />
  )
}

function TabsContent({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn("mt-4 outline-none", className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent }
```

- [ ] **Step 2: Verify TypeScript compiles**

```bash
cd frontend && bun run build 2>&1 | grep -E "error|Error" | head -20
```

Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ui/tabs.tsx
git commit -m "feat: add Tabs UI component (radix-ui wrapper)"
```

---

### Task 5: Workspace API module

**Why:** Centralises all workspace CRUD calls in one place, keeps pages clean.

**Files:**
- Create: `frontend/src/api/workspaces.ts`

- [ ] **Step 1: Create the API module**

```typescript
import { api } from "./client"

// ── Types ──────────────────────────────────────────────────────────────────

export interface WorkspaceDetail {
  id: string
  name: string
  is_auto_created: boolean
  role: "read" | "read_write" | "manage"
  system_prompt: string
  schema_status: "available" | "provisioning" | "unavailable"
  tenant_count: number
  member_count: number
  created_at: string
  updated_at: string
}

export interface WorkspaceMember {
  id: string       // backend returns str(m.id)
  user_id: string  // backend returns str(m.user.id)
  email: string
  name: string
  role: "read" | "read_write" | "manage"
  created_at: string
}

export interface WorkspaceTenant {
  id: string          // WorkspaceTenant UUID
  tenant_id: string   // internal Tenant UUID
  tenant_name: string
  provider: string
}

export interface UserTenant {
  id: string          // TenantMembership UUID
  provider: string
  tenant_id: string   // external ID
  tenant_uuid: string // internal Tenant UUID — use this for workspace API calls
  tenant_name: string
}

// ── Workspace CRUD ─────────────────────────────────────────────────────────

export const workspaceApi = {
  getDetail: (workspaceId: string) =>
    api.get<WorkspaceDetail>(`/api/workspaces/${workspaceId}/`),

  create: (name: string) =>
    api.post<{ id: string; name: string }>("/api/workspaces/", {
      name,
      tenant_ids: [],
    }),

  update: (workspaceId: string, body: { name?: string; system_prompt?: string }) =>
    api.patch<{ id: string; name: string }>(`/api/workspaces/${workspaceId}/`, body),

  delete: (workspaceId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/`),

  // ── Members ──────────────────────────────────────────────────────────────

  getMembers: (workspaceId: string) =>
    api.get<WorkspaceMember[]>(`/api/workspaces/${workspaceId}/members/`),

  updateMember: (workspaceId: string, membershipId: string, role: string) =>
    api.patch<{ id: string; role: string }>(
      `/api/workspaces/${workspaceId}/members/${membershipId}/`,
      { role },
    ),

  removeMember: (workspaceId: string, membershipId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/members/${membershipId}/`),

  // ── Tenants ───────────────────────────────────────────────────────────────

  getTenants: (workspaceId: string) =>
    api.get<WorkspaceTenant[]>(`/api/workspaces/${workspaceId}/tenants/`),

  addTenant: (workspaceId: string, tenantUuid: string) =>
    api.post<{ id: string; tenant_id: string; tenant_name: string }>(
      `/api/workspaces/${workspaceId}/tenants/`,
      { tenant_id: tenantUuid },
    ),

  removeTenant: (workspaceId: string, workspaceTenantId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/tenants/${workspaceTenantId}/`),

  // ── User's available tenants ──────────────────────────────────────────────

  getUserTenants: () => api.get<UserTenant[]>("/api/auth/tenants/"),
}
```

- [ ] **Step 2: Verify TypeScript compiles**

```bash
cd frontend && bun run build 2>&1 | grep -E "error|Error" | head -20
```

Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/workspaces.ts
git commit -m "feat: add workspace API module"
```

---

### Task 6: Router updates

**Files:**
- Modify: `frontend/src/router.tsx`

- [ ] **Step 1: Add workspace routes**

At the top of `frontend/src/router.tsx`, add the two page imports (they don't exist yet — TypeScript will error until the pages are created, so we'll add them now with a comment):

Actually: add the route entries but import the pages lazily using `React.lazy` so missing files don't block the build. OR: create stub page files first (next task), then add the routes. Do this in two sub-steps:

First, create stub files so the imports resolve. These are **temporary stubs** — they will be replaced with full implementations in Tasks 9 and 10 respectively:

**`frontend/src/pages/WorkspacesPage/index.ts`:**
```typescript
export { WorkspacesPage } from "./WorkspacesPage"
```

**`frontend/src/pages/WorkspacesPage/WorkspacesPage.tsx`** (temporary stub, replaced in Task 9):
```typescript
export function WorkspacesPage() {
  return <div>Workspaces</div>
}
```

**`frontend/src/pages/WorkspaceDetailPage/index.ts`:**
```typescript
export { WorkspaceDetailPage } from "./WorkspaceDetailPage"
```

**`frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx`** (temporary stub, replaced in Task 10):
```typescript
export function WorkspaceDetailPage() {
  return <div>Workspace Detail</div>
}
```

Then update `frontend/src/router.tsx` — add imports and routes:

```typescript
// Add with other page imports:
import { WorkspacesPage } from "@/pages/WorkspacesPage"
import { WorkspaceDetailPage } from "@/pages/WorkspaceDetailPage"

// Add inside the children array, before the catch-all:
{ path: "workspaces", element: <WorkspacesPage /> },
{ path: "workspaces/:workspaceId", element: <WorkspaceDetailPage /> },
```

- [ ] **Step 2: Verify build**

```bash
cd frontend && bun run build 2>&1 | grep -E "error|Error" | head -20
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/router.tsx frontend/src/pages/WorkspacesPage/ frontend/src/pages/WorkspaceDetailPage/
git commit -m "feat: add /workspaces and /workspaces/:id routes (stub pages)"
```

---

## Chunk 2: Sidebar + Create Modal

### Task 7: Replace Sidebar Select with DropdownMenu

**Why:** The current `<Select>` only supports value items. We need non-value items ("Manage workspaces…", "+ New workspace") with a divider between them and the workspace list.

**Files:**
- Modify: `frontend/src/components/Sidebar/Sidebar.tsx`

The `<DropdownMenu>` component already exists at `frontend/src/components/ui/dropdown-menu.tsx`.

- [ ] **Step 1: Rewrite the workspace selector section**

Replace the `<Select>` block (the block starting with `<Select` for workspace selection in `Sidebar.tsx`) with a `<DropdownMenu>`. Also add `useState` to the React import at the top of the file — the current file only imports `useEffect`.

Key shape:

```typescript
import { useNavigate } from "react-router-dom"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { ChevronDown } from "lucide-react"

// Add state for modal:
const [showCreateModal, setShowCreateModal] = useState(false)

// Replace the Select block with:
<div className="border-b p-4">
  <label className="text-xs font-medium text-muted-foreground">Workspace</label>
  <DropdownMenu>
    <DropdownMenuTrigger asChild>
      <Button
        variant="outline"
        className="mt-1 w-full justify-between font-normal"
        data-testid="domain-selector"
      >
        <span className="truncate">
          {domains.find((d) => d.id === activeDomainId)?.name ?? "Select workspace"}
        </span>
        <ChevronDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
      </Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent className="w-56">
      {domains.map((d) => (
        <DropdownMenuItem
          key={d.id}
          data-testid={`domain-item-${d.id}`}
          onSelect={() => { setActiveDomain(d.id); newThread() }}
          className={d.id === activeDomainId ? "font-medium" : ""}
        >
          {d.name}
        </DropdownMenuItem>
      ))}
      <DropdownMenuSeparator />
      <DropdownMenuItem onSelect={() => navigate("/workspaces")}>
        Manage workspaces…
      </DropdownMenuItem>
      <DropdownMenuItem onSelect={() => setShowCreateModal(true)}>
        + New workspace
      </DropdownMenuItem>
    </DropdownMenuContent>
  </DropdownMenu>
  {showCreateModal && (
    <CreateWorkspaceModal onClose={() => setShowCreateModal(false)} />
  )}
</div>
```

Import `CreateWorkspaceModal` from `@/components/CreateWorkspaceModal` (created in Task 8).

- [ ] **Step 2: Build to catch TypeScript errors**

```bash
cd frontend && bun run build 2>&1 | grep -E "error|Error" | head -20
```

If `CreateWorkspaceModal` import fails, create a stub (empty component) temporarily so the build passes. The real component comes in Task 8.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/Sidebar/Sidebar.tsx
git commit -m "feat: replace workspace Select with DropdownMenu, add manage/create actions"
```

---

### Task 8: CreateWorkspaceModal

**Files:**
- Create: `frontend/src/components/CreateWorkspaceModal/CreateWorkspaceModal.tsx`
- Create: `frontend/src/components/CreateWorkspaceModal/index.ts`

The modal has one field (name). After creation it navigates to the new workspace's detail page and refreshes the domain list.

- [ ] **Step 1: Create the modal component**

```typescript
// frontend/src/components/CreateWorkspaceModal/CreateWorkspaceModal.tsx
import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { workspaceApi } from "@/api/workspaces"
import { ApiError } from "@/api/client"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

interface Props {
  onClose: () => void
}

export function CreateWorkspaceModal({ onClose }: Props) {
  const navigate = useNavigate()
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)

  const [name, setName] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    setLoading(true)
    setError(null)
    try {
      const workspace = await workspaceApi.create(name.trim())
      await fetchDomains()
      setActiveDomain(workspace.id)
      onClose()
      navigate(`/workspaces/${workspace.id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create workspace")
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="sm:max-w-md" data-testid="create-workspace-modal">
        <DialogHeader>
          <DialogTitle>New Workspace</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit}>
          <div className="py-4">
            <Label htmlFor="workspace-name">Name</Label>
            <Input
              id="workspace-name"
              data-testid="workspace-name-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Acme Corp"
              className="mt-1"
              autoFocus
            />
            {error && <p className="mt-2 text-sm text-destructive">{error}</p>}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!name.trim() || loading}
              data-testid="create-workspace-submit"
            >
              {loading ? "Creating…" : "Create Workspace"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
```

```typescript
// frontend/src/components/CreateWorkspaceModal/index.ts
export { CreateWorkspaceModal } from "./CreateWorkspaceModal"
```

- [ ] **Step 2: Update Sidebar.tsx import**

If you added a stub in Task 6, replace the stub import with the real component. The import line should read:

```typescript
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal"
```

- [ ] **Step 3: Build to verify**

```bash
cd frontend && bun run build 2>&1 | grep -E "error|Error" | head -20
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/CreateWorkspaceModal/ frontend/src/components/Sidebar/Sidebar.tsx
git commit -m "feat: CreateWorkspaceModal with name field, navigates to new workspace on create"
```

---

## Chunk 3: WorkspacesPage

### Task 9: WorkspacesPage — full implementation

**Files:**
- Modify: `frontend/src/pages/WorkspacesPage/WorkspacesPage.tsx`

Replace the stub with the real implementation.

- [ ] **Step 1: Implement WorkspacesPage**

```typescript
import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { TenantMembership } from "@/store/domainSlice"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal"
import { Button } from "@/components/ui/button"
import { Users, Database, ChevronRight } from "lucide-react"

function roleBadge(role: string) {
  const styles: Record<string, string> = {
    manage: "bg-green-950 text-green-400",
    read_write: "bg-blue-950 text-blue-400",
    read: "bg-muted text-muted-foreground",
  }
  const labels: Record<string, string> = {
    manage: "Manager",
    read_write: "Read-Write",
    read: "Read",
  }
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${styles[role] ?? styles.read}`}
    >
      {labels[role] ?? role}
    </span>
  )
}

function WorkspaceRow({ workspace, onClick }: { workspace: TenantMembership; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      data-testid={`workspace-row-${workspace.id}`}
      className="flex w-full items-center justify-between rounded-lg border bg-card px-4 py-3 text-left transition-colors hover:bg-accent"
    >
      <div>
        <div className="font-medium">{workspace.name}</div>
        <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <Users className="h-3 w-3" />
            {workspace.member_count} {workspace.member_count === 1 ? "member" : "members"}
          </span>
          <span className="flex items-center gap-1">
            <Database className="h-3 w-3" />
            {workspace.tenant_count} {workspace.tenant_count === 1 ? "source" : "sources"}
          </span>
        </div>
      </div>
      <div className="flex items-center gap-3">
        {roleBadge(workspace.role)}
        <ChevronRight className="h-4 w-4 text-muted-foreground" />
      </div>
    </button>
  )
}

export function WorkspacesPage() {
  const navigate = useNavigate()
  const domains = useAppStore((s) => s.domains)
  const domainsStatus = useAppStore((s) => s.domainsStatus)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const [showCreate, setShowCreate] = useState(false)

  // Ensure fresh data when landing on this page
  // (domains are loaded in Sidebar, but may be stale)
  // Only re-fetch if idle; Sidebar handles the initial load
  const isLoading = domainsStatus === "loading"

  return (
    <div className="mx-auto max-w-2xl px-6 py-8">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold" data-testid="workspaces-title">Workspaces</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Your workspaces across connected data sources
          </p>
        </div>
        <Button onClick={() => setShowCreate(true)} data-testid="new-workspace-btn">
          New workspace
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-16 animate-pulse rounded-lg border bg-muted" />
          ))}
        </div>
      ) : domains.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center">
          <p className="text-muted-foreground">No workspaces yet.</p>
          <Button className="mt-4" onClick={() => setShowCreate(true)}>
            Create your first workspace
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          {domains.map((ws) => (
            <WorkspaceRow
              key={ws.id}
              workspace={ws}
              onClick={() => navigate(`/workspaces/${ws.id}`)}
            />
          ))}
        </div>
      )}

      {showCreate && (
        <CreateWorkspaceModal
          onClose={() => {
            setShowCreate(false)
            fetchDomains()
          }}
        />
      )}
    </div>
  )
}
```

- [ ] **Step 2: Build to verify**

```bash
cd frontend && bun run build 2>&1 | grep -E "error|Error" | head -20
```

- [ ] **Step 3: Lint**

```bash
cd frontend && bun run lint 2>&1 | grep -v "^$" | head -20
```

Fix any warnings.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/WorkspacesPage/WorkspacesPage.tsx
git commit -m "feat: WorkspacesPage — list workspaces with role badges and create modal"
```

---

## Chunk 4: WorkspaceDetailPage

### Task 10: WorkspaceDetailPage skeleton + Members tab

**Files:**
- Modify: `frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx`

Replace the stub. This task covers the page shell (header, tabs structure, back nav) and the fully-implemented Members tab. Tenants and Settings tabs are stubs here.

- [ ] **Step 1: Implement page shell + Members tab**

```typescript
import { useState, useEffect, useCallback } from "react"
import { Link, useParams, useNavigate } from "react-router-dom"
import { workspaceApi } from "@/api/workspaces"
import type { WorkspaceDetail, WorkspaceMember } from "@/api/workspaces"
import { ApiError } from "@/api/client"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ChevronLeft } from "lucide-react"

// ── Role badge ──────────────────────────────────────────────────────────────

function RoleBadge({ role }: { role: string }) {
  const styles: Record<string, string> = {
    manage: "bg-green-950 text-green-400",
    read_write: "bg-blue-950 text-blue-400",
    read: "bg-muted text-muted-foreground",
  }
  const labels: Record<string, string> = {
    manage: "Manager",
    read_write: "Read-Write",
    read: "Read",
  }
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${styles[role] ?? styles.read}`}>
      {labels[role] ?? role}
    </span>
  )
}

// ── Members Tab ─────────────────────────────────────────────────────────────

function MembersTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [updatingId, setUpdatingId] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await workspaceApi.getMembers(workspaceId)
      setMembers(data)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load members")
    } finally {
      setLoading(false)
    }
  }, [workspaceId])

  useEffect(() => { load() }, [load])

  async function handleRoleChange(membershipId: string, newRole: string) {
    setUpdatingId(membershipId)
    try {
      await workspaceApi.updateMember(workspaceId, membershipId, newRole)
      setMembers((prev) =>
        prev.map((m) => (m.id === membershipId ? { ...m, role: newRole as WorkspaceMember["role"] } : m))
      )
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Failed to update role")
    } finally {
      setUpdatingId(null)
    }
  }

  async function handleRemove(membershipId: string) {
    if (!confirm("Remove this member from the workspace?")) return
    setRemovingId(membershipId)
    try {
      await workspaceApi.removeMember(workspaceId, membershipId)
      setMembers((prev) => prev.filter((m) => m.id !== membershipId))
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Failed to remove member")
    } finally {
      setRemovingId(null)
    }
  }

  if (loading) return <div className="py-8 text-center text-muted-foreground">Loading…</div>
  if (error) return <div className="py-8 text-center text-destructive">{error}</div>

  return (
    <div data-testid="members-tab">
      <div className="mb-4 flex items-center justify-between">
        <span className="text-sm text-muted-foreground">
          {members.length} {members.length === 1 ? "member" : "members"}
        </span>
      </div>
      <div className="rounded-lg border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50">
              <th className="px-4 py-2 text-left font-medium text-muted-foreground">User</th>
              <th className="px-4 py-2 text-left font-medium text-muted-foreground">Role</th>
              {isManager && <th className="px-4 py-2" />}
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.id} className="border-b last:border-0" data-testid={`member-row-${member.id}`}>
                <td className="px-4 py-3">
                  <div className="font-medium">{member.name || member.email}</div>
                  <div className="text-xs text-muted-foreground">{member.email}</div>
                </td>
                <td className="px-4 py-3">
                  {isManager ? (
                    <Select
                      value={member.role}
                      onValueChange={(v) => handleRoleChange(member.id, v)}
                      disabled={updatingId === member.id}
                    >
                      <SelectTrigger className="h-8 w-32" data-testid={`member-role-${member.id}`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="read">Read</SelectItem>
                        <SelectItem value="read_write">Read-Write</SelectItem>
                        <SelectItem value="manage">Manager</SelectItem>
                      </SelectContent>
                    </Select>
                  ) : (
                    <RoleBadge role={member.role} />
                  )}
                </td>
                {isManager && (
                  <td className="px-4 py-3 text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-destructive hover:text-destructive"
                      onClick={() => handleRemove(member.id)}
                      disabled={removingId === member.id}
                      data-testid={`remove-member-${member.id}`}
                    >
                      Remove
                    </Button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Page ────────────────────────────────────────────────────────────────────

export function WorkspaceDetailPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>()
  const navigate = useNavigate()
  const [workspace, setWorkspace] = useState<WorkspaceDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!workspaceId) return
    setLoading(true)
    workspaceApi
      .getDetail(workspaceId)
      .then(setWorkspace)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load workspace"))
      .finally(() => setLoading(false))
  }, [workspaceId])

  if (loading) return <div className="p-8 text-center text-muted-foreground">Loading…</div>
  if (error || !workspace) return <div className="p-8 text-center text-destructive">{error ?? "Workspace not found"}</div>

  const isManager = workspace.role === "manage"

  return (
    <div className="mx-auto max-w-2xl px-6 py-8">
      {/* Header */}
      <div className="mb-6">
        <Link
          to="/workspaces"
          className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          data-testid="back-to-workspaces"
        >
          <ChevronLeft className="h-4 w-4" />
          Workspaces
        </Link>
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-semibold" data-testid="workspace-name">
            {workspace.name}
          </h1>
          <RoleBadge role={workspace.role} />
        </div>
      </div>

      {/* Tabs */}
      <Tabs defaultValue="members">
        <TabsList data-testid="workspace-tabs">
          <TabsTrigger value="members" data-testid="tab-members">Members</TabsTrigger>
          <TabsTrigger value="tenants" data-testid="tab-tenants">Tenants</TabsTrigger>
          <TabsTrigger value="settings" data-testid="tab-settings">Settings</TabsTrigger>
        </TabsList>

        <TabsContent value="members">
          <MembersTab workspaceId={workspace.id} isManager={isManager} />
        </TabsContent>

        <TabsContent value="tenants">
          <div className="py-8 text-center text-muted-foreground">Tenants (coming soon)</div>
        </TabsContent>

        <TabsContent value="settings">
          <div className="py-8 text-center text-muted-foreground">Settings (coming soon)</div>
        </TabsContent>
      </Tabs>
    </div>
  )
}
```

- [ ] **Step 2: Build to verify**

```bash
cd frontend && bun run build 2>&1 | grep -E "error|Error" | head -20
```

- [ ] **Step 3: Lint**

```bash
cd frontend && bun run lint 2>&1 | head -20
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx
git commit -m "feat: WorkspaceDetailPage — shell + Members tab with role editing and removal"
```

---

### Task 11: Tenants tab

**Files:**
- Modify: `frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx`

Replace the Tenants tab stub with the full implementation.

- [ ] **Step 1: Create a `TenantsTab` component and add it to the file**

Add the following component to the WorkspaceDetailPage file (before the `WorkspaceDetailPage` function), then replace the `<TabsContent value="tenants">` stub with `<TenantsTab workspaceId={workspace.id} isManager={isManager} />`:

```typescript
import type { WorkspaceTenant, UserTenant } from "@/api/workspaces"

function TenantsTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
  const [tenants, setTenants] = useState<WorkspaceTenant[]>([])
  const [userTenants, setUserTenants] = useState<UserTenant[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [addingId, setAddingId] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)
  const [showAdd, setShowAdd] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [wsTenants, allTenants] = await Promise.all([
        workspaceApi.getTenants(workspaceId),
        workspaceApi.getUserTenants(),
      ])
      setTenants(wsTenants)
      setUserTenants(allTenants)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load tenants")
    } finally {
      setLoading(false)
    }
  }, [workspaceId])

  useEffect(() => { load() }, [load])

  // Tenants the user has access to that are not already in this workspace
  const inWorkspaceIds = new Set(tenants.map((t) => t.tenant_id))
  const available = userTenants.filter((t) => !inWorkspaceIds.has(t.tenant_uuid))

  async function handleAdd(tenantUuid: string) {
    setAddingId(tenantUuid)
    try {
      await workspaceApi.addTenant(workspaceId, tenantUuid)
      // Re-fetch instead of using the partial response (backend doesn't return `provider`)
      await load()
      setShowAdd(false)
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Failed to add data source")
    } finally {
      setAddingId(null)
    }
  }

  async function handleRemove(wt: WorkspaceTenant) {
    if (!confirm(`Remove "${wt.tenant_name}" from this workspace?`)) return
    setRemovingId(wt.id)
    try {
      await workspaceApi.removeTenant(workspaceId, wt.id)
      setTenants((prev) => prev.filter((t) => t.id !== wt.id))
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Failed to remove data source")
    } finally {
      setRemovingId(null)
    }
  }

  if (loading) return <div className="py-8 text-center text-muted-foreground">Loading…</div>
  if (error) return <div className="py-8 text-center text-destructive">{error}</div>

  return (
    <div data-testid="tenants-tab">
      <div className="mb-4 flex items-center justify-between">
        <span className="text-sm text-muted-foreground">
          {tenants.length} connected {tenants.length === 1 ? "source" : "sources"}
        </span>
        {isManager && available.length > 0 && (
          <Button size="sm" variant="outline" onClick={() => setShowAdd((v) => !v)} data-testid="add-tenant-btn">
            + Add data source
          </Button>
        )}
      </div>

      {showAdd && available.length > 0 && (
        <div className="mb-4 rounded-lg border bg-muted/30 p-4">
          <p className="mb-2 text-sm font-medium">Available data sources</p>
          <div className="space-y-2">
            {available.map((t) => (
              <div key={t.tenant_uuid} className="flex items-center justify-between">
                <div>
                  <span className="text-sm">{t.tenant_name}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{t.provider}</span>
                </div>
                <Button
                  size="sm"
                  onClick={() => handleAdd(t.tenant_uuid)}
                  disabled={addingId === t.tenant_uuid}
                  data-testid={`add-tenant-${t.tenant_uuid}`}
                >
                  {addingId === t.tenant_uuid ? "Adding…" : "Add"}
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}

      {tenants.length === 0 ? (
        <div className="rounded-lg border border-dashed p-8 text-center text-muted-foreground">
          No data sources connected.
        </div>
      ) : (
        <div className="rounded-lg border">
          {tenants.map((t, i) => (
            <div
              key={t.id}
              className={`flex items-center justify-between px-4 py-3 ${i < tenants.length - 1 ? "border-b" : ""}`}
              data-testid={`tenant-row-${t.id}`}
            >
              <div>
                <div className="font-medium">{t.tenant_name}</div>
                <div className="text-xs text-muted-foreground">{t.provider}</div>
              </div>
              {isManager && tenants.length > 1 && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-destructive hover:text-destructive"
                  onClick={() => handleRemove(t)}
                  disabled={removingId === t.id}
                  data-testid={`remove-tenant-${t.id}`}
                >
                  Remove
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
```

Note: Import `useCallback` at the top of the file if not already imported. Add `WorkspaceTenant` and `UserTenant` to the import from `@/api/workspaces`.

- [ ] **Step 2: Build to verify**

```bash
cd frontend && bun run build 2>&1 | grep -E "error|Error" | head -20
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx
git commit -m "feat: WorkspaceDetailPage — Tenants tab with add/remove data sources"
```

---

### Task 12: Settings tab

**Files:**
- Modify: `frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx`

Replace the Settings tab stub. Also sync the sidebar after rename/delete.

- [ ] **Step 1: Create a `SettingsTab` component and add it to the file**

```typescript
function SettingsTab({
  workspace,
  onRename,
  onDelete,
}: {
  workspace: WorkspaceDetail
  onRename: (newName: string) => void
  onDelete: () => void
}) {
  const [name, setName] = useState(workspace.name)
  const [systemPrompt, setSystemPrompt] = useState(workspace.system_prompt ?? "")
  const [savingName, setSavingName] = useState(false)
  const [savingPrompt, setSavingPrompt] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [nameError, setNameError] = useState<string | null>(null)
  const [promptError, setPromptError] = useState<string | null>(null)

  const isManager = workspace.role === "manage"

  async function handleSaveName(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim() || name.trim() === workspace.name) return
    setSavingName(true)
    setNameError(null)
    try {
      await workspaceApi.update(workspace.id, { name: name.trim() })
      onRename(name.trim())
    } catch (err) {
      setNameError(err instanceof ApiError ? err.message : "Failed to rename workspace")
    } finally {
      setSavingName(false)
    }
  }

  async function handleSavePrompt(e: React.FormEvent) {
    e.preventDefault()
    setSavingPrompt(true)
    setPromptError(null)
    try {
      await workspaceApi.update(workspace.id, { system_prompt: systemPrompt })
    } catch (err) {
      setPromptError(err instanceof ApiError ? err.message : "Failed to save system prompt")
    } finally {
      setSavingPrompt(false)
    }
  }

  async function handleDelete() {
    if (!confirm(`Delete "${workspace.name}"? This cannot be undone.`)) return
    setDeleting(true)
    try {
      await workspaceApi.delete(workspace.id)
      onDelete()
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Failed to delete workspace")
      setDeleting(false)
    }
  }

  return (
    <div className="space-y-8" data-testid="settings-tab">
      {/* Rename */}
      <section>
        <h3 className="mb-3 text-sm font-medium">Workspace name</h3>
        <form onSubmit={handleSaveName} className="flex items-start gap-3">
          <div className="flex-1">
            <input
              className="w-full rounded-md border bg-background px-3 py-2 text-sm disabled:opacity-50"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={!isManager}
              data-testid="settings-name-input"
            />
            {nameError && <p className="mt-1 text-xs text-destructive">{nameError}</p>}
          </div>
          {isManager && (
            <Button
              type="submit"
              size="sm"
              disabled={savingName || !name.trim() || name.trim() === workspace.name}
              data-testid="settings-save-name"
            >
              {savingName ? "Saving…" : "Save"}
            </Button>
          )}
        </form>
      </section>

      {/* System Prompt */}
      <section>
        <h3 className="mb-1 text-sm font-medium">System prompt</h3>
        <p className="mb-3 text-xs text-muted-foreground">
          Custom instructions for the AI agent in this workspace.
        </p>
        <form onSubmit={handleSavePrompt} className="space-y-2">
          <textarea
            className="w-full rounded-md border bg-background px-3 py-2 text-sm disabled:opacity-50"
            rows={6}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            disabled={!isManager}
            placeholder="Leave blank for default behavior…"
            data-testid="settings-system-prompt"
          />
          {promptError && <p className="text-xs text-destructive">{promptError}</p>}
          {isManager && (
            <Button
              type="submit"
              size="sm"
              disabled={savingPrompt}
              data-testid="settings-save-prompt"
            >
              {savingPrompt ? "Saving…" : "Save system prompt"}
            </Button>
          )}
        </form>
      </section>

      {/* Danger Zone */}
      {isManager && (
        <section className="rounded-lg border border-destructive/30 p-4">
          <h3 className="mb-1 text-sm font-medium text-destructive">Danger zone</h3>
          <p className="mb-3 text-xs text-muted-foreground">
            Permanently delete this workspace and all its threads. This cannot be undone.
          </p>
          <Button
            variant="destructive"
            size="sm"
            onClick={handleDelete}
            disabled={deleting}
            data-testid="delete-workspace-btn"
          >
            {deleting ? "Deleting…" : "Delete workspace"}
          </Button>
        </section>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Wire up SettingsTab in WorkspaceDetailPage**

In the `WorkspaceDetailPage` function, replace the Settings tab stub and add `onRename`/`onDelete` handlers:

```typescript
// Add inside WorkspaceDetailPage, alongside the other state:
const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)

function handleRename(newName: string) {
  setWorkspace((prev) => prev ? { ...prev, name: newName } : prev)
  fetchDomains()  // sync sidebar
}

function handleDelete() {
  fetchDomains()  // removes from sidebar
  navigate("/workspaces")
}

// Replace Settings tab content:
<TabsContent value="settings">
  <SettingsTab workspace={workspace} onRename={handleRename} onDelete={handleDelete} />
</TabsContent>
```

Also add `useAppStore` import if not already present.

- [ ] **Step 3: Build to verify**

```bash
cd frontend && bun run build 2>&1 | grep -E "error|Error" | head -20
```

- [ ] **Step 4: Lint**

```bash
cd frontend && bun run lint 2>&1 | head -20
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/WorkspaceDetailPage/WorkspaceDetailPage.tsx
git commit -m "feat: WorkspaceDetailPage — Settings tab with rename, system prompt, and delete"
```

---

## Final verification

- [ ] Start the full dev stack: `uv run honcho -f Procfile.dev start`
- [ ] Navigate to the app at `http://localhost:5173`
- [ ] Open sidebar dropdown — confirm "Manage workspaces…" and "+ New workspace" appear
- [ ] Click "+ New workspace" — modal opens, create a workspace, confirm redirect to detail page
- [ ] Click "Manage workspaces…" — `/workspaces` list page shows all workspaces with role badges
- [ ] Click a workspace — detail page loads with tabs
- [ ] Members tab: role dropdown visible for manager role, read-only badges for others
- [ ] Tenants tab: lists tenants, "Add data source" button appears for manager
- [ ] Settings tab: rename and system prompt save; delete navigates back to list
- [ ] Run backend tests: `uv run pytest --tb=short -q`
- [ ] Run frontend lint: `cd frontend && bun run lint`

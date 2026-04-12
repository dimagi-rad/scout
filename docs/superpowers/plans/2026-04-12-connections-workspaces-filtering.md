# Connections & Workspaces List Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add search and provider filtering to the Connections page (as a compact table with dialog forms) and the Workspaces page (with search + role/provider filters), sharing a `SearchFilterBar` component.

**Architecture:** Extract a reusable `SearchFilterBar` component used by both pages. Refactor ConnectionsPage from cards to a shadcn Table with Dialog-based add/edit. Extend the workspace list API to include tenant details (replacing `tenant_count`), then add filtering to WorkspacesPage.

**Tech Stack:** React 19, TypeScript, shadcn/ui (Table, Dialog, Badge, Button, Input), Django REST Framework

---

## File Structure

### New files
- `frontend/src/components/SearchFilterBar/SearchFilterBar.tsx` — reusable search input + toggle-button filter groups

### Modified files
- `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx` — card list → table, inline forms → dialog, add SearchFilterBar
- `frontend/src/pages/WorkspacesPage/WorkspacesPage.tsx` — add SearchFilterBar, show tenant details in rows, filtering logic
- `frontend/src/api/workspaces.ts` — replace `tenant_count: number` with `tenants` array on `WorkspaceListItem`
- `frontend/src/store/domainSlice.ts` — `TenantMembership` type alias inherits `tenants` from updated `WorkspaceListItem`
- `apps/workspaces/api/workspace_views.py` — `WorkspaceListView.get` nests tenant details instead of `tenant_count`
- `tests/test_workspace_management.py` — update assertions for new response shape

---

### Task 1: SearchFilterBar component

**Files:**
- Create: `frontend/src/components/SearchFilterBar/SearchFilterBar.tsx`

- [ ] **Step 1: Create the SearchFilterBar component**

```tsx
// frontend/src/components/SearchFilterBar/SearchFilterBar.tsx
import { Search } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"

export interface FilterOption {
  value: string
  label: string
  count?: number
}

export interface FilterGroup {
  name: string
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

export function SearchFilterBar({
  search,
  onSearchChange,
  placeholder = "Search...",
  filters,
  activeFilters,
  onFilterChange,
}: SearchFilterBarProps) {
  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
      <div className="relative flex-1">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder={placeholder}
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          className="pl-9"
          data-testid="search-filter-input"
        />
      </div>
      {filters.map((group) => (
        <div key={group.name} className="flex flex-wrap gap-2">
          <Button
            variant={activeFilters[group.name] == null ? "default" : "outline"}
            size="sm"
            onClick={() => onFilterChange(group.name, null)}
          >
            All
          </Button>
          {group.options.map((opt) => (
            <Button
              key={opt.value}
              variant={activeFilters[group.name] === opt.value ? "default" : "outline"}
              size="sm"
              onClick={() => onFilterChange(group.name, opt.value)}
              data-testid={`filter-${group.name}-${opt.value}`}
            >
              {opt.label}
              {opt.count != null && (
                <span className="ml-1 text-xs opacity-60">{opt.count}</span>
              )}
            </Button>
          ))}
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors related to SearchFilterBar

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/SearchFilterBar/SearchFilterBar.tsx
git commit -m "feat: add SearchFilterBar component for reusable search + filter toolbar"
```

---

### Task 2: ConnectionsPage — table layout with SearchFilterBar

**Files:**
- Modify: `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx`

This task replaces the card-based domain list with a shadcn Table and adds SearchFilterBar for search + provider filtering. The OAuth Providers section stays unchanged at the top.

- [ ] **Step 1: Rewrite ConnectionsPage with table and filtering**

Replace the entire contents of `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx` with:

```tsx
import { useState, useEffect, useCallback, useMemo } from "react"
import { api } from "@/api/client"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  SearchFilterBar,
  type FilterGroup,
} from "@/components/SearchFilterBar/SearchFilterBar"

interface OAuthProvider {
  id: string
  name: string
  login_url: string
  connected: boolean
  status?: "connected" | "expired" | "disconnected" | null
}

interface ApiKeyDomain {
  membership_id: string
  provider: string
  tenant_id: string
  tenant_name: string
  credential_type: string
}

const providerBadgeStyles: Record<string, string> = {
  commcare: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  dhis2: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
}

function ProviderBadge({ provider }: { provider: string }) {
  return (
    <Badge
      variant="secondary"
      className={providerBadgeStyles[provider] ?? "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400"}
    >
      {provider}
    </Badge>
  )
}

export function ConnectionsPage() {
  const fetchStoreDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const storeDomains = useAppStore((s) => s.domains)
  const [providers, setProviders] = useState<OAuthProvider[]>([])
  const [domains, setDomains] = useState<ApiKeyDomain[]>([])
  const [loadingProviders, setLoadingProviders] = useState(true)
  const [loadingDomains, setLoadingDomains] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [disconnecting, setDisconnecting] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)

  // Search and filter state
  const [search, setSearch] = useState("")
  const [activeFilters, setActiveFilters] = useState<Record<string, string | null>>({
    provider: null,
  })

  // Dialog state
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editingDomain, setEditingDomain] = useState<ApiKeyDomain | null>(null)

  // Form state
  const [formDomain, setFormDomain] = useState("")
  const [formUsername, setFormUsername] = useState("")
  const [formApiKey, setFormApiKey] = useState("")
  const [formLoading, setFormLoading] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const fetchProviders = useCallback(async () => {
    setLoadingProviders(true)
    try {
      const data = await api.get<{ providers: OAuthProvider[] }>("/api/auth/providers/")
      setProviders(data.providers)
    } catch {
      setError("Failed to load OAuth providers.")
    } finally {
      setLoadingProviders(false)
    }
  }, [])

  const fetchDomains = useCallback(async () => {
    setLoadingDomains(true)
    try {
      const data = await api.get<ApiKeyDomain[]>("/api/auth/tenant-credentials/")
      setDomains(data)
    } catch {
      setError("Failed to load connected domains.")
    } finally {
      setLoadingDomains(false)
    }
  }, [])

  useEffect(() => {
    fetchProviders()
    fetchDomains()
  }, [fetchProviders, fetchDomains])

  // Derived filter options from domain list
  const providerFilterGroup = useMemo((): FilterGroup => {
    const counts = new Map<string, number>()
    for (const d of domains) {
      counts.set(d.provider, (counts.get(d.provider) ?? 0) + 1)
    }
    return {
      name: "provider",
      options: [...counts.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([value, count]) => ({ value, label: value, count })),
    }
  }, [domains])

  // Filtered domains
  const filteredDomains = useMemo(() => {
    const lowerSearch = search.toLowerCase()
    return domains.filter((d) => {
      if (activeFilters.provider && d.provider !== activeFilters.provider) return false
      if (
        lowerSearch &&
        !d.tenant_name.toLowerCase().includes(lowerSearch) &&
        !d.tenant_id.toLowerCase().includes(lowerSearch)
      ) {
        return false
      }
      return true
    })
  }, [domains, search, activeFilters])

  function handleFilterChange(group: string, value: string | null) {
    setActiveFilters((prev) => ({ ...prev, [group]: value }))
  }

  function openAddDialog() {
    setEditingDomain(null)
    setFormDomain("")
    setFormUsername("")
    setFormApiKey("")
    setFormError(null)
    setDialogOpen(true)
  }

  function openEditDialog(domain: ApiKeyDomain) {
    setEditingDomain(domain)
    setFormDomain(domain.tenant_id)
    setFormUsername("")
    setFormApiKey("")
    setFormError(null)
    setDialogOpen(true)
  }

  function closeDialog() {
    setDialogOpen(false)
    setEditingDomain(null)
    setFormError(null)
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setFormLoading(true)
    setFormError(null)
    try {
      if (editingDomain) {
        const body: Record<string, string> = { tenant_name: formDomain }
        if (formUsername && formApiKey) {
          body.credential = `${formUsername}:${formApiKey}`
        }
        await api.patch(`/api/auth/tenant-credentials/${editingDomain.membership_id}/`, body)
      } else {
        await api.post("/api/auth/tenant-credentials/", {
          provider: "commcare",
          tenant_id: formDomain,
          tenant_name: formDomain,
          credential: `${formUsername}:${formApiKey}`,
        })
      }
      await fetchDomains()
      void fetchStoreDomains()
      closeDialog()
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Failed to save domain.")
    } finally {
      setFormLoading(false)
    }
  }

  async function confirmRemove(membershipId: string) {
    setRemoving(membershipId)
    setConfirmRemoveId(null)
    setError(null)
    try {
      await api.delete(`/api/auth/tenant-credentials/${membershipId}/`)
      await fetchDomains()
      await fetchStoreDomains()
      if (activeDomainId === membershipId) {
        const next = storeDomains.find((d) => d.id !== membershipId)
        if (next) setActiveDomain(next.id)
      }
    } catch {
      setError("Failed to remove domain.")
    } finally {
      setRemoving(null)
    }
  }

  async function handleDisconnect(providerId: string) {
    setDisconnecting(providerId)
    setError(null)
    try {
      await api.post(`/api/auth/providers/${providerId}/disconnect/`)
      await fetchProviders()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to disconnect provider.")
    } finally {
      setDisconnecting(null)
    }
  }

  return (
    <div className="p-6 space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Connected Accounts</h1>
        <p className="text-sm text-muted-foreground">
          Manage your external account connections.
        </p>
      </div>

      {error && (
        <p className="text-sm text-destructive" data-testid="connections-error">
          {error}
        </p>
      )}

      {/* OAuth Providers section */}
      <section className="space-y-4">
        <h2 className="text-lg font-medium">OAuth Providers</h2>
        {loadingProviders ? (
          <p className="text-sm text-muted-foreground">Loading providers...</p>
        ) : providers.length === 0 ? (
          <p className="text-sm text-muted-foreground">No OAuth providers configured.</p>
        ) : (
          providers.map((provider) => (
            <Card key={provider.id}>
              <CardContent className="flex items-center justify-between p-4">
                <div>
                  <p className="font-medium">{provider.name}</p>
                  <p className={`text-sm ${provider.status === "expired" ? "text-amber-600" : "text-muted-foreground"}`}>
                    {provider.status === "connected"
                      ? "Connected"
                      : provider.status === "expired"
                        ? "Connection expired"
                        : "Not connected"}
                  </p>
                </div>
                {provider.status === "connected" ? (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleDisconnect(provider.id)}
                    disabled={disconnecting === provider.id}
                    data-testid={`disconnect-${provider.id}`}
                  >
                    {disconnecting === provider.id ? "Disconnecting..." : "Disconnect"}
                  </Button>
                ) : (
                  <Button variant="outline" size="sm" asChild data-testid={`connect-${provider.id}`}>
                    <a href={`${provider.login_url}?process=connect&next=/settings/connections`}>
                      {provider.status === "expired" ? "Reconnect" : "Connect"}
                    </a>
                  </Button>
                )}
              </CardContent>
            </Card>
          ))
        )}
      </section>

      {/* API Key Domains section */}
      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-medium">API Key Domains</h2>
          <Button
            size="sm"
            variant="outline"
            onClick={openAddDialog}
            data-testid="add-domain-button"
          >
            Add Domain
          </Button>
        </div>

        {loadingDomains ? (
          <p className="text-sm text-muted-foreground">Loading domains...</p>
        ) : (
          <>
            {domains.length > 0 && (
              <SearchFilterBar
                search={search}
                onSearchChange={setSearch}
                placeholder="Search tenants..."
                filters={providerFilterGroup.options.length > 1 ? [providerFilterGroup] : []}
                activeFilters={activeFilters}
                onFilterChange={handleFilterChange}
              />
            )}

            {filteredDomains.length === 0 ? (
              <div className="rounded-lg border border-dashed p-8 text-center">
                <p className="text-muted-foreground">
                  {domains.length === 0
                    ? "No API key domains connected."
                    : "No tenants match your search."}
                </p>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Provider</TableHead>
                    <TableHead>Tenant ID</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredDomains.map((domain) => {
                    const isConfirming = confirmRemoveId === domain.membership_id

                    if (isConfirming) {
                      return (
                        <TableRow key={domain.membership_id}>
                          <TableCell colSpan={4}>
                            <div className="flex items-center justify-between">
                              <p className="text-sm font-medium">
                                Remove <span className="font-semibold">{domain.tenant_name || domain.tenant_id}</span>? This cannot be undone.
                              </p>
                              <div className="flex gap-2">
                                <Button
                                  variant="outline"
                                  size="sm"
                                  onClick={() => setConfirmRemoveId(null)}
                                  data-testid={`cancel-remove-${domain.tenant_id}`}
                                >
                                  Cancel
                                </Button>
                                <Button
                                  variant="destructive"
                                  size="sm"
                                  onClick={() => confirmRemove(domain.membership_id)}
                                  disabled={removing === domain.membership_id}
                                  data-testid={`confirm-remove-${domain.tenant_id}`}
                                >
                                  {removing === domain.membership_id ? "Removing..." : "Confirm Remove"}
                                </Button>
                              </div>
                            </div>
                          </TableCell>
                        </TableRow>
                      )
                    }

                    return (
                      <TableRow key={domain.membership_id}>
                        <TableCell className="font-medium" data-testid={`domain-name-${domain.tenant_id}`}>
                          {domain.tenant_name || domain.tenant_id}
                        </TableCell>
                        <TableCell>
                          <ProviderBadge provider={domain.provider} />
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {domain.tenant_id}
                        </TableCell>
                        <TableCell className="text-right">
                          <div className="flex justify-end gap-2">
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => openEditDialog(domain)}
                              data-testid={`edit-domain-${domain.tenant_id}`}
                            >
                              Edit
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="text-destructive hover:text-destructive"
                              onClick={() => setConfirmRemoveId(domain.membership_id)}
                              data-testid={`remove-domain-${domain.tenant_id}`}
                            >
                              Remove
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            )}
          </>
        )}
      </section>

      {/* Add/Edit Domain Dialog */}
      <DomainDialog
        open={dialogOpen}
        editing={editingDomain}
        formDomain={formDomain}
        formUsername={formUsername}
        formApiKey={formApiKey}
        formLoading={formLoading}
        formError={formError}
        onDomainChange={setFormDomain}
        onUsernameChange={setFormUsername}
        onApiKeyChange={setFormApiKey}
        onSubmit={handleSubmit}
        onClose={closeDialog}
      />
    </div>
  )
}

// -- Dialog sub-component (kept in same file since it's tightly coupled) --

interface DomainDialogProps {
  open: boolean
  editing: ApiKeyDomain | null
  formDomain: string
  formUsername: string
  formApiKey: string
  formLoading: boolean
  formError: string | null
  onDomainChange: (v: string) => void
  onUsernameChange: (v: string) => void
  onApiKeyChange: (v: string) => void
  onSubmit: (e: React.FormEvent) => void
  onClose: () => void
}

function DomainDialog({
  open,
  editing,
  formDomain,
  formUsername,
  formApiKey,
  formLoading,
  formError,
  onDomainChange,
  onUsernameChange,
  onApiKeyChange,
  onSubmit,
  onClose,
}: DomainDialogProps) {
  const isEdit = editing !== null

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose() }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit Domain" : "Add Domain"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "Update the domain name or credentials."
              : "Connect a new CommCare domain with API key credentials."}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="dialog-domain">CommCare Domain</Label>
            <Input
              id="dialog-domain"
              data-testid="domain-form-domain"
              required
              placeholder="my-project"
              value={formDomain}
              onChange={(e) => onDomainChange(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="dialog-username">
              CommCare Username{isEdit ? " (leave blank to keep existing)" : ""}
            </Label>
            <Input
              id="dialog-username"
              data-testid="domain-form-username"
              type="email"
              required={!isEdit}
              placeholder="you@example.com"
              value={formUsername}
              onChange={(e) => onUsernameChange(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="dialog-api-key">
              API Key{isEdit ? " (leave blank to keep existing)" : ""}
            </Label>
            <Input
              id="dialog-api-key"
              data-testid="domain-form-api-key"
              type="password"
              required={!isEdit}
              value={formApiKey}
              onChange={(e) => onApiKeyChange(e.target.value)}
            />
          </div>
          {formError && (
            <p className="text-sm text-destructive" data-testid="domain-form-error">
              {formError}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={formLoading}>
              {formLoading ? "Saving..." : isEdit ? "Save Changes" : "Add Domain"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
```

**Important notes for the implementer:**
- The `ProviderBadge` helper and `DomainDialog` sub-component stay in the same file — they are tightly coupled to this page.
- The page layout uses `p-6` (flush-left, per project convention in `frontend-layout.md`), replacing the previous `container mx-auto px-8 py-8`.
- The `SearchFilterBar` is hidden when there's only one provider (no point filtering).
- The `data-testid` attributes from the original implementation are preserved so QA scenarios don't break.

- [ ] **Step 2: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Run ESLint**

Run: `cd frontend && bun run lint`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx
git commit -m "feat: refactor ConnectionsPage to compact table with search/filter and dialog forms"
```

---

### Task 3: Backend — include tenant details in workspace list API

**Files:**
- Modify: `apps/workspaces/api/workspace_views.py:38-59`
- Modify: `tests/test_workspace_management.py:52-59`

- [ ] **Step 1: Update the test to expect the new response shape**

In `tests/test_workspace_management.py`, update the `test_list_includes_role_and_counts` test:

```python
# Replace the existing test_list_includes_role_and_counts method (lines 52-59) with:
    def test_list_includes_role_and_tenants(self, client, user, workspace, tenant):
        client.force_login(user)
        resp = client.get("/api/workspaces/")
        assert resp.status_code == 200
        entry = next(w for w in resp.json() if w["id"] == str(workspace.id))
        assert entry["role"] == WorkspaceRole.MANAGE
        assert entry["member_count"] == 1
        assert len(entry["tenants"]) == 1
        assert entry["tenants"][0]["tenant_name"] == tenant.canonical_name
        assert entry["tenants"][0]["provider"] == tenant.provider
```

Also update the assertion in `TestWorkspaceCreate.test_create_workspace` (around line 113) that checks `tenant_count`:

Find: `assert resp.json()["tenant_count"] == 0`
Replace with: `assert resp.json()["tenants"] == []`

- [ ] **Step 2: Run the updated tests to confirm they fail**

Run: `uv run pytest tests/test_workspace_management.py::TestWorkspaceList::test_list_includes_role_and_tenants tests/test_workspace_management.py::TestWorkspaceCreate::test_create_workspace -v`
Expected: FAIL — response still has `tenant_count` instead of `tenants`

- [ ] **Step 3: Update WorkspaceListView.get to nest tenant details**

In `apps/workspaces/api/workspace_views.py`, replace the `get` method of `WorkspaceListView` (lines 38-59):

```python
    def get(self, request):
        memberships = (
            WorkspaceMembership.objects.filter(user=request.user)
            .select_related("workspace")
            .prefetch_related("workspace__workspace_tenants__tenant")
            .annotate(
                member_count=Count("workspace__memberships", distinct=True),
            )
        )
        results = []
        for m in memberships:
            tenants = [
                {
                    "id": str(wt.tenant.id),
                    "tenant_name": wt.tenant.canonical_name,
                    "provider": wt.tenant.provider,
                }
                for wt in m.workspace.workspace_tenants.all()
            ]
            results.append(
                {
                    "id": str(m.workspace.id),
                    "name": m.workspace.name,
                    "is_auto_created": m.workspace.is_auto_created,
                    "role": m.role,
                    "tenants": tenants,
                    "member_count": m.member_count,
                    "created_at": m.workspace.created_at.isoformat(),
                }
            )
        return Response(results)
```

Also update the `post` method response (around line 96-107) to return `tenants` instead of `tenant_count`:

```python
        # Build tenants list for the response
        workspace_tenants = WorkspaceTenant.objects.filter(
            workspace=workspace
        ).select_related("tenant")
        tenants = [
            {
                "id": str(wt.tenant.id),
                "tenant_name": wt.tenant.canonical_name,
                "provider": wt.tenant.provider,
            }
            for wt in workspace_tenants
        ]

        return Response(
            {
                "id": str(workspace.id),
                "name": workspace.name,
                "is_auto_created": workspace.is_auto_created,
                "role": WorkspaceRole.MANAGE,
                "tenants": tenants,
                "member_count": 1,
                "created_at": workspace.created_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )
```

Remove the now-unused `Count` import for `tenant_count` — but keep `Count` since `member_count` still uses it.

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_workspace_management.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add apps/workspaces/api/workspace_views.py tests/test_workspace_management.py
git commit -m "feat: include tenant details in workspace list API response"
```

---

### Task 4: Frontend — update WorkspaceListItem type

**Files:**
- Modify: `frontend/src/api/workspaces.ts:10-16`

- [ ] **Step 1: Update WorkspaceListItem type**

In `frontend/src/api/workspaces.ts`, replace the `WorkspaceListItem` interface (lines 10-16):

```ts
export interface WorkspaceListTenant {
  id: string
  tenant_name: string
  provider: string
}

export interface WorkspaceListItem {
  id: string
  name: string
  is_auto_created: boolean
  role: "read" | "read_write" | "manage"
  tenants: WorkspaceListTenant[]
  member_count: number
  created_at: string
}
```

- [ ] **Step 2: Check for compile errors**

Run: `cd frontend && npx tsc --noEmit 2>&1 | head -30`

Expected: Errors in `WorkspacesPage.tsx` referencing `tenant_count` — this is expected and will be fixed in Task 5.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/workspaces.ts
git commit -m "feat: update WorkspaceListItem type to include tenant details"
```

---

### Task 5: WorkspacesPage — add search, filtering, and tenant details

**Files:**
- Modify: `frontend/src/pages/WorkspacesPage/WorkspacesPage.tsx`

- [ ] **Step 1: Rewrite WorkspacesPage with SearchFilterBar and tenant details**

Replace the entire contents of `frontend/src/pages/WorkspacesPage/WorkspacesPage.tsx` with:

```tsx
import { useState, useMemo } from "react"
import { useNavigate } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { TenantMembership } from "@/store/domainSlice"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal"
import { RoleBadge } from "@/components/RoleBadge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Users, ChevronRight } from "lucide-react"
import {
  SearchFilterBar,
  type FilterGroup,
} from "@/components/SearchFilterBar/SearchFilterBar"

const providerBadgeStyles: Record<string, string> = {
  commcare: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  dhis2: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
}

const MAX_VISIBLE_TENANTS = 4

function TenantList({ tenants }: { tenants: { id: string; tenant_name: string; provider: string }[] }) {
  const visible = tenants.slice(0, MAX_VISIBLE_TENANTS)
  const overflow = tenants.length - MAX_VISIBLE_TENANTS

  return (
    <div className="flex flex-wrap items-center gap-1">
      {visible.map((t) => (
        <Badge
          key={t.id}
          variant="secondary"
          className={providerBadgeStyles[t.provider] ?? "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400"}
        >
          {t.tenant_name}
        </Badge>
      ))}
      {overflow > 0 && (
        <Badge variant="outline" className="text-xs">
          +{overflow} more
        </Badge>
      )}
    </div>
  )
}

function WorkspaceRow({ workspace, onClick }: { workspace: TenantMembership; onClick: () => void }) {
  const tenants = workspace.tenants ?? []

  return (
    <button
      onClick={onClick}
      data-testid={`workspace-row-${workspace.id}`}
      className="flex w-full items-center justify-between rounded-lg border bg-card px-4 py-3 text-left transition-colors hover:bg-accent"
    >
      <div className="min-w-0 flex-1">
        <div className="font-medium">{workspace.name}</div>
        <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <Users className="h-3 w-3" />
            {workspace.member_count} {workspace.member_count === 1 ? "member" : "members"}
          </span>
        </div>
        {tenants.length > 0 && (
          <div className="mt-2">
            <TenantList tenants={tenants} />
          </div>
        )}
      </div>
      <div className="flex items-center gap-3">
        <RoleBadge role={workspace.role} />
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

  // Search and filter state
  const [search, setSearch] = useState("")
  const [activeFilters, setActiveFilters] = useState<Record<string, string | null>>({
    role: null,
    provider: null,
  })

  const isLoading = domainsStatus === "loading" || domainsStatus === "idle"

  // Derive filter groups from workspace data
  const filterGroups = useMemo((): FilterGroup[] => {
    const groups: FilterGroup[] = []

    // Role filter
    const roleCounts = new Map<string, number>()
    for (const ws of domains) {
      roleCounts.set(ws.role, (roleCounts.get(ws.role) ?? 0) + 1)
    }
    if (roleCounts.size > 1) {
      const roleLabels: Record<string, string> = {
        read: "Read",
        read_write: "Read+Write",
        manage: "Manage",
      }
      groups.push({
        name: "role",
        options: [...roleCounts.entries()]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([value, count]) => ({
            value,
            label: roleLabels[value] ?? value,
            count,
          })),
      })
    }

    // Provider filter
    const providerCounts = new Map<string, number>()
    for (const ws of domains) {
      const tenants = ws.tenants ?? []
      const providers = new Set(tenants.map((t) => t.provider))
      for (const p of providers) {
        providerCounts.set(p, (providerCounts.get(p) ?? 0) + 1)
      }
    }
    if (providerCounts.size > 1) {
      groups.push({
        name: "provider",
        options: [...providerCounts.entries()]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([value, count]) => ({ value, label: value, count })),
      })
    }

    return groups
  }, [domains])

  // Filtered workspaces
  const filtered = useMemo(() => {
    const lowerSearch = search.toLowerCase()
    return domains.filter((ws) => {
      if (lowerSearch && !ws.name.toLowerCase().includes(lowerSearch)) return false
      if (activeFilters.role && ws.role !== activeFilters.role) return false
      if (activeFilters.provider) {
        const tenants = ws.tenants ?? []
        if (!tenants.some((t) => t.provider === activeFilters.provider)) return false
      }
      return true
    })
  }, [domains, search, activeFilters])

  function handleFilterChange(group: string, value: string | null) {
    setActiveFilters((prev) => ({ ...prev, [group]: value }))
  }

  return (
    <div className="p-6">
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
      ) : domainsStatus === "error" ? (
        <div className="rounded-lg border border-destructive/20 p-6 text-center">
          <p className="text-sm text-destructive">Failed to load workspaces.</p>
          <button
            className="mt-2 text-sm text-muted-foreground underline hover:text-foreground"
            onClick={() => fetchDomains()}
          >
            Try again
          </button>
        </div>
      ) : domains.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center">
          <p className="text-muted-foreground">No workspaces yet.</p>
          <Button className="mt-4" onClick={() => setShowCreate(true)}>
            Create your first workspace
          </Button>
        </div>
      ) : (
        <div className="space-y-4">
          {(filterGroups.length > 0 || domains.length > 5) && (
            <SearchFilterBar
              search={search}
              onSearchChange={setSearch}
              placeholder="Search workspaces..."
              filters={filterGroups}
              activeFilters={activeFilters}
              onFilterChange={handleFilterChange}
            />
          )}

          {filtered.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center">
              <p className="text-muted-foreground">No workspaces match your search.</p>
            </div>
          ) : (
            <div className="space-y-2">
              {filtered.map((ws) => (
                <WorkspaceRow
                  key={ws.id}
                  workspace={ws}
                  onClick={() => navigate(`/workspaces/${ws.id}`)}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {showCreate && (
        <CreateWorkspaceModal onClose={() => setShowCreate(false)} />
      )}
    </div>
  )
}
```

**Notes for implementer:**
- `TenantMembership` inherits from `WorkspaceListItem` which now has `tenants` instead of `tenant_count`. The `Database` icon import is removed since we no longer show a generic sources count.
- `SearchFilterBar` is only shown when there are filter groups (multiple roles or providers) or when the list has more than 5 items (to avoid cluttering small lists).
- The `TenantList` sub-component shows up to 4 tenant badges, with a "+N more" overflow badge.
- The `tenants ?? []` fallback handles the `TenantMembership` type alias which has `tenants` as potentially undefined during the migration.

- [ ] **Step 2: Verify it compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Run ESLint**

Run: `cd frontend && bun run lint`
Expected: No errors

- [ ] **Step 4: Build to confirm production build works**

Run: `cd frontend && bun run build`
Expected: Build succeeds

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/WorkspacesPage/WorkspacesPage.tsx
git commit -m "feat: add search/filter to WorkspacesPage with tenant details in rows"
```

---

### Task 6: Verify end-to-end

This is a manual verification task — start the dev servers and test the changes in a browser.

- [ ] **Step 1: Start the dev servers**

Run: `uv run honcho -f Procfile.dev start`

- [ ] **Step 2: Test ConnectionsPage**

Navigate to `/settings/connections` and verify:
- OAuth Providers section appears at the top
- API Key Domains section shows a table with Name, Provider, Tenant ID, Actions columns
- Provider badges appear in the Provider column
- Search filters the table by name and tenant ID
- Provider toggle buttons appear when there are multiple providers
- Clicking "Add Domain" opens a dialog
- Clicking "Edit" on a row opens a dialog pre-filled with domain name
- Clicking "Remove" shows inline confirmation in the table row
- Empty state shows when no results match

- [ ] **Step 3: Test WorkspacesPage**

Navigate to `/workspaces` and verify:
- Search bar and filter buttons appear when there are workspaces
- Search filters by workspace name
- Role filter buttons appear when there are multiple roles
- Provider filter buttons appear when workspaces have tenants from different providers
- Each workspace row shows tenant names with provider badges
- Overflow indicator appears for workspaces with 5+ tenants
- Clicking a workspace row navigates to the detail page

- [ ] **Step 4: Commit any fixes needed**

If any issues were found and fixed during manual testing, commit them:

```bash
git add -u
git commit -m "fix: address issues found during manual testing of connections/workspaces filtering"
```

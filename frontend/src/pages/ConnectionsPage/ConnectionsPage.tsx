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

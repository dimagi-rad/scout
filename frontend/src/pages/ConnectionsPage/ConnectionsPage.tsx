import { useState, useEffect, useCallback } from "react"
import { api } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

interface OAuthProvider {
  id: string
  name: string
  login_url: string
  connected: boolean
}

interface ApiKeyDomain {
  membership_id: string
  provider: string
  tenant_id: string
  tenant_name: string
  credential_type: string
}

type FormMode = "hidden" | "add" | { editing: ApiKeyDomain }

export function ConnectionsPage() {
  const [providers, setProviders] = useState<OAuthProvider[]>([])
  const [domains, setDomains] = useState<ApiKeyDomain[]>([])
  const [loadingProviders, setLoadingProviders] = useState(true)
  const [loadingDomains, setLoadingDomains] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [disconnecting, setDisconnecting] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [formMode, setFormMode] = useState<FormMode>("hidden")

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

  function openAddForm() {
    setFormDomain("")
    setFormUsername("")
    setFormApiKey("")
    setFormError(null)
    setFormMode("add")
  }

  function openEditForm(domain: ApiKeyDomain) {
    setFormDomain(domain.tenant_id)
    setFormUsername("")
    setFormApiKey("")
    setFormError(null)
    setFormMode({ editing: domain })
  }

  function cancelForm() {
    setFormMode("hidden")
    setFormError(null)
  }

  async function handleAddDomain(e: React.FormEvent) {
    e.preventDefault()
    setFormLoading(true)
    setFormError(null)
    try {
      await api.post("/api/auth/tenant-credentials/", {
        provider: "commcare",
        tenant_id: formDomain,
        tenant_name: formDomain,
        credential: `${formUsername}:${formApiKey}`,
      })
      await fetchDomains()
      setFormMode("hidden")
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Failed to add domain.")
    } finally {
      setFormLoading(false)
    }
  }

  async function handleEditDomain(e: React.FormEvent) {
    e.preventDefault()
    if (typeof formMode !== "object") return
    setFormLoading(true)
    setFormError(null)
    const { membership_id } = formMode.editing
    try {
      const body: Record<string, string> = { tenant_name: formDomain }
      if (formUsername && formApiKey) {
        body.credential = `${formUsername}:${formApiKey}`
      }
      await api.patch(`/api/auth/tenant-credentials/${membership_id}/`, body)
      await fetchDomains()
      setFormMode("hidden")
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Failed to update domain.")
    } finally {
      setFormLoading(false)
    }
  }

  async function handleRemoveDomain(membershipId: string) {
    if (!confirm("Remove this domain? This cannot be undone.")) return
    setRemoving(membershipId)
    setError(null)
    try {
      await api.delete(`/api/auth/tenant-credentials/${membershipId}/`)
      await fetchDomains()
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
    } catch {
      setError("Failed to disconnect provider.")
    } finally {
      setDisconnecting(null)
    }
  }

  const isEditing = typeof formMode === "object"

  return (
    <div className="mx-auto max-w-2xl space-y-8 p-6">
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

      {/* CommCare Domains section */}
      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-medium">CommCare Domains (API Key)</h2>
          {formMode === "hidden" && (
            <Button
              size="sm"
              variant="outline"
              onClick={openAddForm}
              data-testid="add-domain-button"
            >
              Add Domain
            </Button>
          )}
        </div>

        {loadingDomains ? (
          <p className="text-sm text-muted-foreground">Loading domains...</p>
        ) : domains.length === 0 && formMode === "hidden" ? (
          <p className="text-sm text-muted-foreground">No API key domains connected.</p>
        ) : null}

        {domains.map((domain) => (
          <Card key={domain.membership_id}>
            <CardContent className="flex items-center justify-between p-4">
              <div>
                <p className="font-medium" data-testid={`domain-name-${domain.tenant_id}`}>
                  {domain.tenant_name || domain.tenant_id}
                </p>
                <p className="text-sm text-muted-foreground">{domain.tenant_id}</p>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => openEditForm(domain)}
                  data-testid={`edit-domain-${domain.tenant_id}`}
                >
                  Edit
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleRemoveDomain(domain.membership_id)}
                  disabled={removing === domain.membership_id}
                  data-testid={`remove-domain-${domain.tenant_id}`}
                >
                  {removing === domain.membership_id ? "Removing..." : "Remove"}
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}

        {/* Add / Edit form */}
        {formMode !== "hidden" && (
          <Card data-testid="domain-form">
            <CardContent className="p-4">
              <form
                onSubmit={isEditing ? handleEditDomain : handleAddDomain}
                className="space-y-4"
              >
                <p className="font-medium">{isEditing ? "Edit Domain" : "Add Domain"}</p>
                <div className="space-y-2">
                  <Label htmlFor="form-domain">CommCare Domain</Label>
                  <Input
                    id="form-domain"
                    data-testid="domain-form-domain"
                    required
                    placeholder="my-project"
                    value={formDomain}
                    onChange={(e) => setFormDomain(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="form-username">
                    CommCare Username{isEditing ? " (leave blank to keep existing)" : ""}
                  </Label>
                  <Input
                    id="form-username"
                    data-testid="domain-form-username"
                    type="email"
                    required={!isEditing}
                    placeholder="you@example.com"
                    value={formUsername}
                    onChange={(e) => setFormUsername(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="form-api-key">
                    API Key{isEditing ? " (leave blank to keep existing)" : ""}
                  </Label>
                  <Input
                    id="form-api-key"
                    data-testid="domain-form-api-key"
                    type="password"
                    required={!isEditing}
                    value={formApiKey}
                    onChange={(e) => setFormApiKey(e.target.value)}
                  />
                </div>
                {formError && (
                  <p className="text-sm text-destructive" data-testid="domain-form-error">
                    {formError}
                  </p>
                )}
                <div className="flex gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    className="flex-1"
                    onClick={cancelForm}
                  >
                    Cancel
                  </Button>
                  <Button type="submit" className="flex-1" disabled={formLoading}>
                    {formLoading ? "Saving..." : isEditing ? "Save Changes" : "Add Domain"}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        )}
      </section>

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
                  <p className="text-sm text-muted-foreground">
                    {provider.connected ? "Connected" : "Not connected"}
                  </p>
                </div>
                {provider.connected ? (
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
                      Connect
                    </a>
                  </Button>
                )}
              </CardContent>
            </Card>
          ))
        )}
      </section>
    </div>
  )
}

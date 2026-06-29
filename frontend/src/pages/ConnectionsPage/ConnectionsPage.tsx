import { useState, useEffect, useCallback, useMemo } from "react"
import { api } from "@/api/client"
import { BASE_PATH } from "@/config"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  SearchFilterBar,
  type FilterGroup,
} from "@/components/SearchFilterBar/SearchFilterBar"
import {
  ApiConnectionDialog,
  type ApiKeyConnection,
} from "@/components/ApiConnectionDialog"

interface OAuthProvider {
  id: string
  name: string
  login_url: string
  connected: boolean
  status?: "connected" | "expired" | "disconnected" | null
}

const providerBadgeStyles: Record<string, string> = {
  commcare: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  commcare_connect: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  ocs: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400",
}

function ProviderBadge({ provider }: { provider: string }) {
  return (
    <Badge
      variant="secondary"
      className={
        providerBadgeStyles[provider] ??
        "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400"
      }
    >
      {provider}
    </Badge>
  )
}

function teamLabelFor(conn: ApiKeyConnection): string {
  const named = conn.chatbots.find((cb) => cb.team_name)
  return named?.team_name || conn.provider
}

type DialogState =
  | { mode: "add" }
  | { mode: "edit"; editing: ApiKeyConnection }
  | null

export function ConnectionsPage() {
  const fetchStoreDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const [providers, setProviders] = useState<OAuthProvider[]>([])
  const [connections, setConnections] = useState<ApiKeyConnection[]>([])
  const [loadingProviders, setLoadingProviders] = useState(true)
  const [loadingConnections, setLoadingConnections] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [disconnecting, setDisconnecting] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)

  const [search, setSearch] = useState("")
  const [activeFilters, setActiveFilters] = useState<Record<string, string | null>>({
    provider: null,
  })

  const [dialogState, setDialogState] = useState<DialogState>(null)

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

  const fetchConnections = useCallback(async () => {
    setLoadingConnections(true)
    try {
      const data = await api.get<ApiKeyConnection[]>("/api/auth/connections/")
      setConnections(data)
    } catch {
      setError("Failed to load connections.")
    } finally {
      setLoadingConnections(false)
    }
  }, [])

  useEffect(() => {
    fetchProviders()
    fetchConnections()
  }, [fetchProviders, fetchConnections])

  const providerFilterGroup = useMemo((): FilterGroup => {
    const counts = new Map<string, number>()
    for (const c of connections) {
      counts.set(c.provider, (counts.get(c.provider) ?? 0) + 1)
    }
    return {
      name: "provider",
      options: [...counts.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([value, count]) => ({ value, label: value, count })),
    }
  }, [connections])

  const filteredConnections = useMemo(() => {
    const lowerSearch = search.toLowerCase()
    return connections.filter((c) => {
      if (activeFilters.provider && c.provider !== activeFilters.provider) return false
      if (lowerSearch) {
        const matches = c.chatbots.some(
          (cb) =>
            cb.tenant_name.toLowerCase().includes(lowerSearch) ||
            cb.tenant_id.toLowerCase().includes(lowerSearch),
        )
        if (!matches) return false
      }
      return true
    })
  }, [connections, search, activeFilters])

  function handleFilterChange(group: string, value: string | null) {
    setActiveFilters((prev) => ({ ...prev, [group]: value }))
  }

  async function confirmRemove(connection: ApiKeyConnection) {
    const connectionId = connection.connection_id
    setRemoving(connectionId)
    setConfirmRemoveId(null)
    setError(null)
    try {
      await api.delete(`/api/auth/connections/${connectionId}/`)
      await fetchConnections()
      await fetchStoreDomains()
      // Removing a connection can drop the workspaces backed only by it. The
      // connection payload carries no workspace id (chatbots hold
      // TenantMembership ids, a disjoint id space from `activeDomainId`), so we
      // can't tell from `connection` which workspaces vanished. Instead, read
      // the freshly-refetched workspace list (not the stale closure snapshot)
      // and switch away only if the active workspace no longer exists.
      const freshDomains = useAppStore.getState().domains
      if (
        activeDomainId != null &&
        !freshDomains.some((d) => d.id === activeDomainId)
      ) {
        const next = freshDomains[0]
        if (next) setActiveDomain(next.id)
      }
    } catch {
      setError("Failed to remove connection.")
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
                  <p
                    className={`text-sm ${
                      provider.status === "expired"
                        ? "text-amber-600"
                        : "text-muted-foreground"
                    }`}
                  >
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
                  <Button
                    variant="outline"
                    size="sm"
                    asChild
                    data-testid={`connect-${provider.id}`}
                  >
                    <a
                      href={`${BASE_PATH}${provider.login_url}?process=connect&next=${BASE_PATH}/settings/connections`}
                    >
                      {provider.status === "expired" ? "Reconnect" : "Connect"}
                    </a>
                  </Button>
                )}
              </CardContent>
            </Card>
          ))
        )}
      </section>

      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-medium">Connections</h2>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setDialogState({ mode: "add" })}
            data-testid="add-connection-button"
          >
            Add API Connection
          </Button>
        </div>

        {loadingConnections ? (
          <p className="text-sm text-muted-foreground">Loading connections...</p>
        ) : (
          <>
            {connections.length > 0 && (
              <SearchFilterBar
                search={search}
                onSearchChange={setSearch}
                placeholder="Search chatbots..."
                filters={
                  providerFilterGroup.options.length > 1 ? [providerFilterGroup] : []
                }
                activeFilters={activeFilters}
                onFilterChange={handleFilterChange}
              />
            )}

            {filteredConnections.length === 0 ? (
              <div className="rounded-lg border border-dashed p-8 text-center">
                <p className="text-muted-foreground">
                  {connections.length === 0
                    ? "No connections."
                    : "No connections match your search."}
                </p>
              </div>
            ) : (
              <div className="space-y-4">
                {filteredConnections.map((conn) => {
                  const isApiKey = conn.credential_type === "api_key"
                  const isConfirming = confirmRemoveId === conn.connection_id
                  const teamLabel = teamLabelFor(conn)

                  return (
                    <Card
                      key={conn.connection_id}
                      data-testid={`connection-card-${conn.connection_id}`}
                    >
                      <CardContent className="space-y-4 p-4">
                        <div className="flex items-start justify-between gap-4">
                          <div className="space-y-1">
                            <div className="flex items-center gap-2">
                              <p
                                className="font-medium"
                                data-testid={`connection-team-${conn.connection_id}`}
                              >
                                {teamLabel}
                              </p>
                            </div>
                            <div className="flex items-center gap-2">
                              <ProviderBadge provider={conn.provider} />
                              <Badge variant="secondary">
                                {isApiKey ? "API Key" : "OAuth"}
                              </Badge>
                            </div>
                          </div>
                          {isApiKey && !isConfirming && (
                            <div className="flex shrink-0 gap-2">
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() =>
                                  setDialogState({ mode: "edit", editing: conn })
                                }
                                data-testid={`edit-connection-${conn.connection_id}`}
                              >
                                Edit
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="text-destructive hover:text-destructive"
                                onClick={() => setConfirmRemoveId(conn.connection_id)}
                                data-testid={`remove-connection-${conn.connection_id}`}
                              >
                                Remove
                              </Button>
                            </div>
                          )}
                        </div>

                        {isConfirming && (
                          <div className="flex items-center justify-between gap-4 rounded-md border border-destructive/30 bg-destructive/5 p-3">
                            <p className="text-sm font-medium">
                              Remove{" "}
                              <span className="font-semibold">{teamLabel}</span>? Its
                              chatbots will be hidden. This cannot be undone.
                            </p>
                            <div className="flex shrink-0 gap-2">
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => setConfirmRemoveId(null)}
                                data-testid={`cancel-remove-${conn.connection_id}`}
                              >
                                Cancel
                              </Button>
                              <Button
                                variant="destructive"
                                size="sm"
                                onClick={() => confirmRemove(conn)}
                                disabled={removing === conn.connection_id}
                                data-testid={`confirm-remove-${conn.connection_id}`}
                              >
                                {removing === conn.connection_id
                                  ? "Removing..."
                                  : "Confirm Remove"}
                              </Button>
                            </div>
                          </div>
                        )}

                        <ul className="space-y-1 border-t pt-3">
                          {conn.chatbots.map((cb) => (
                            <li
                              key={cb.membership_id}
                              className="flex items-center justify-between gap-4 text-sm"
                            >
                              <span className="font-medium">
                                {cb.tenant_name || cb.tenant_id}
                              </span>
                              <span className="text-muted-foreground">
                                {cb.tenant_id}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </CardContent>
                    </Card>
                  )
                })}
              </div>
            )}
          </>
        )}
      </section>

      <ApiConnectionDialog
        open={dialogState !== null}
        mode={dialogState?.mode ?? "add"}
        editing={dialogState?.mode === "edit" ? dialogState.editing : null}
        onClose={() => setDialogState(null)}
        onSaved={async () => {
          await fetchConnections()
          void fetchStoreDomains()
        }}
      />
    </div>
  )
}

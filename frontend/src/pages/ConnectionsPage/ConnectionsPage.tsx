import { useState, useEffect, useCallback, useMemo } from "react"
import { api } from "@/api/client"
import { refreshUserTenants } from "@/api/userTenantsCache"
import { CONNECTIONS_PATH } from "@/lib/routes"
import { oauthConnectUrl, type OAuthProvider, type OAuthProviderStatus } from "@/lib/oauth"
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
import { accessNotice, productName, providerAccessLines } from "./accessCopy"

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
  // A CommCare connection's scope is its HQ server, not a team; naming it keeps a
  // www and an EU card (and their remove confirmations) apart.
  if (conn.provider === "commcare" && conn.scope_key) {
    return `CommCare HQ (${conn.scope_label || conn.scope_key})`
  }
  // scope_label is the credential's own team; the chatbot fallback covers
  // connections created before the scope was recorded on the connection.
  if (conn.scope_label) return conn.scope_label
  const named = conn.chatbots.find((cb) => cb.team_name)
  return named?.team_name || conn.provider
}

const WARNING_BADGE =
  "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400"

/** Status line and connect-button label per provider status. */
const PROVIDER_STATUS_COPY: Partial<
  Record<OAuthProviderStatus, { label: string; action: string | null; warn: boolean }>
> = {
  connected: { label: "Connected", action: "Connect", warn: false },
  // No action: reconnecting can't fix a provider blip, so offering it would mislead (#779).
  unavailable: {
    label: "Connected, but we couldn't check right now. Try again later.",
    action: null,
    warn: true,
  },
  expired: { label: "Connection expired", action: "Reconnect", warn: true },
  needs_team: { label: "No team selected", action: "Connect a team", warn: true },
}
const NOT_CONNECTED = { label: "Not connected", action: "Connect", warn: false }

/** Badges for connections that need the user to act, keyed by status. */
const CONNECTION_STATUS_BADGE: Partial<
  Record<NonNullable<ApiKeyConnection["status"]>, { label: string; testId: string }>
> = {
  expired: { label: "Reconnect needed", testId: "connection-expired" },
  needs_team: { label: "No team: remove it and connect a team", testId: "connection-needs-team" },
}

function connectUrlFor(provider: OAuthProvider): string {
  return oauthConnectUrl(provider, CONNECTIONS_PATH)
}

type DialogState =
  | { mode: "add" }
  | { mode: "edit"; editing: ApiKeyConnection }
  | null

export function ConnectionsPage() {
  const fetchStoreDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const userId = useAppStore((s) => s.user?.id)
  const [refreshingSources, setRefreshingSources] = useState(false)
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
    void fetchProviders().then(fetchConnections)
  }, [fetchProviders, fetchConnections])

  const providerForConnection = useMemo(() => {
    const byId = new Map<string, OAuthProvider>()
    for (const p of providers) for (const id of p.connection_ids ?? []) byId.set(id, p)
    return byId
  }, [providers])

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
    const lowerSearch = search.trim().replace(/^#/, "").toLowerCase()
    return connections.filter((c) => {
      if (activeFilters.provider && c.provider !== activeFilters.provider) return false
      if (lowerSearch) {
        // Search what the card shows: its team heading and provider, not just chatbots,
        // so connections with no chatbots stay findable.
        const haystacks = [
          teamLabelFor(c),
          c.provider,
          ...[...c.chatbots, ...(c.archived_chatbots ?? [])].flatMap((cb) => [
            cb.tenant_name,
            cb.tenant_id,
          ]),
        ]
        if (!haystacks.some((h) => h.toLowerCase().includes(lowerSearch))) return false
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
      await fetchProviders()
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

  async function handleRefreshSources() {
    if (!userId) return
    setRefreshingSources(true)
    setError(null)
    try {
      await refreshUserTenants(userId)
      await Promise.all([fetchConnections(), fetchStoreDomains()])
    } catch {
      setError("Failed to refresh sources.")
    } finally {
      setRefreshingSources(false)
    }
  }

  async function handleDisconnect(providerId: string) {
    setDisconnecting(providerId)
    setError(null)
    try {
      await api.post(`/api/auth/providers/${providerId}/disconnect/`)
      await fetchProviders()
      await fetchConnections()
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
          providers.map((provider) => {
            const copy = (provider.status && PROVIDER_STATUS_COPY[provider.status]) || NOT_CONNECTED
            // A team-less sign-in's token is live, so it must stay disconnectable (#379);
            // so is one we merely couldn't refresh (#779).
            const canDisconnect =
              provider.status === "connected" ||
              provider.status === "needs_team" ||
              provider.status === "unavailable"
            const ids = new Set(provider.connection_ids ?? [])
            const accessLines = providerAccessLines(
              provider.name,
              connections
                .filter((c) => ids.has(c.connection_id))
                // The status line already says the sign-in expired.
                .filter((c) => !(provider.status === "expired" && c.access_state === "expired"))
                .map((conn) => ({ conn, teamLabel: teamLabelFor(conn) })),
            )
            return (
              <Card key={provider.id}>
                <CardContent className="flex items-center justify-between p-4">
                  <div>
                    <p className="font-medium">{provider.name}</p>
                    <p
                      className={`text-sm ${copy.warn ? "text-amber-600" : "text-muted-foreground"}`}
                      data-testid={`provider-status-${provider.id}`}
                    >
                      {copy.label}
                    </p>
                    {accessLines.map((line) => (
                      <p
                        key={line}
                        className="text-sm text-amber-600"
                        data-testid={`provider-access-${provider.id}`}
                      >
                        {line}
                      </p>
                    ))}
                  </div>
                  <div className="flex shrink-0 gap-2">
                    {(provider.status === "connected" || provider.status === "unavailable") &&
                      provider.supports_multiple_scopes && (
                        <Button
                          variant="outline"
                          size="sm"
                          asChild
                          data-testid={`connect-another-${provider.id}`}
                        >
                          <a href={connectUrlFor(provider)}>Connect another team</a>
                        </Button>
                      )}
                    {provider.status !== "connected" && copy.action && (
                      <Button
                        variant="outline"
                        size="sm"
                        asChild
                        data-testid={`connect-${provider.id}`}
                      >
                        <a href={connectUrlFor(provider)}>{copy.action}</a>
                      </Button>
                    )}
                    {canDisconnect && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleDisconnect(provider.id)}
                        disabled={disconnecting === provider.id}
                        data-testid={`disconnect-${provider.id}`}
                      >
                        {disconnecting === provider.id ? "Disconnecting..." : "Disconnect all"}
                      </Button>
                    )}
                  </div>
                </CardContent>
              </Card>
            )
          })
        )}
      </section>

      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-medium">Connections</h2>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={handleRefreshSources}
              disabled={refreshingSources || !userId}
              data-testid="refresh-sources-button"
            >
              {refreshingSources ? "Refreshing..." : "Refresh sources"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setDialogState({ mode: "add" })}
              data-testid="add-connection-button"
            >
              Add API Connection
            </Button>
          </div>
        </div>

        {loadingConnections ? (
          <p className="text-sm text-muted-foreground">Loading connections...</p>
        ) : (
          <>
            {connections.length > 0 && (
              <SearchFilterBar
                search={search}
                onSearchChange={setSearch}
                placeholder="Search connections..."
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
                  const statusBadge = conn.status ? CONNECTION_STATUS_BADGE[conn.status] : undefined
                  const notice = accessNotice(conn, teamLabel)
                  const oauthProvider = providerForConnection.get(conn.connection_id)
                  const archived = conn.archived_chatbots ?? []

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
                              {statusBadge && (
                                <Badge
                                  variant="secondary"
                                  className={WARNING_BADGE}
                                  data-testid={`${statusBadge.testId}-${conn.connection_id}`}
                                >
                                  {statusBadge.label}
                                </Badge>
                              )}
                            </div>
                          </div>
                          {!isConfirming && (
                            <div className="flex shrink-0 gap-2">
                              {isApiKey && (
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
                              )}
                              {/* Removing one OAuth connection disconnects that team
                                  only, leaving the user's other teams signed in. */}
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

                        {notice && (
                          <div
                            className="space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-300"
                            data-testid={`connection-access-${conn.connection_id}`}
                            data-access-state={conn.access_state}
                          >
                            <p className="font-medium">{notice.title}</p>
                            <p>{notice.body}</p>
                            {notice.offerReconnect && oauthProvider && (
                              <Button
                                variant="outline"
                                size="sm"
                                asChild
                                data-testid={`connection-reconnect-${conn.connection_id}`}
                              >
                                <a href={connectUrlFor(oauthProvider)}>Reconnect</a>
                              </Button>
                            )}
                          </div>
                        )}

                        {isConfirming && (
                          <div className="flex items-center justify-between gap-4 rounded-md border border-destructive/30 bg-destructive/5 p-3">
                            <p className="text-sm font-medium">
                              Remove{" "}
                              <span className="font-semibold">{teamLabel}</span>? Its
                              chatbots will be hidden and its saved credentials removed. You can reconnect later.
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
                          {archived.map((cb) => (
                            <li
                              key={cb.membership_id}
                              className="flex items-center justify-between gap-4 text-sm text-muted-foreground"
                              data-testid={`connection-chatbot-no-access-${cb.membership_id}`}
                            >
                              <span className="flex items-center gap-2">
                                <span className="font-medium">{cb.tenant_name || cb.tenant_id}</span>
                                {cb.archived_reason === "unlisted" ? (
                                  <Badge variant="outline" title={`${productName(conn.provider)} no longer lists it`}>
                                    No longer listed
                                  </Badge>
                                ) : (
                                  <Badge variant="secondary" className={WARNING_BADGE}>
                                    No access
                                  </Badge>
                                )}
                              </span>
                              <span>{cb.tenant_id}</span>
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

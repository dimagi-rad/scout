import { useState, useEffect, useCallback } from "react"
import { api } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"

interface OAuthProvider {
  id: string
  name: string
  login_url: string
  connected: boolean
}

export function ConnectionsPage() {
  const [providers, setProviders] = useState<OAuthProvider[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [disconnecting, setDisconnecting] = useState<string | null>(null)

  const fetchProviders = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.get<OAuthProvider[]>("/api/auth/providers/")
      setProviders(data)
    } catch {
      setError("Failed to load OAuth providers.")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchProviders()
  }, [fetchProviders])

  async function handleDisconnect(providerId: string) {
    setDisconnecting(providerId)
    try {
      await api.post(`/api/auth/providers/${providerId}/disconnect/`)
      await fetchProviders()
    } catch {
      setError("Failed to disconnect provider.")
    } finally {
      setDisconnecting(null)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8">
        <p className="text-sm text-muted-foreground">Loading providers...</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center p-8">
        <p className="text-sm text-destructive" data-testid="connections-error">
          {error}
        </p>
      </div>
    )
  }

  if (providers.length === 0) {
    return (
      <div className="flex items-center justify-center p-8">
        <p className="text-sm text-muted-foreground">
          No OAuth providers configured.
        </p>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Connected Accounts</h1>
        <p className="text-sm text-muted-foreground">
          Manage your external account connections.
        </p>
      </div>
      <div className="space-y-4">
        {providers.map((provider) => (
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
                  {disconnecting === provider.id
                    ? "Disconnecting..."
                    : "Disconnect"}
                </Button>
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  asChild
                  data-testid={`connect-${provider.id}`}
                >
                  <a
                    href={`${provider.login_url}?process=connect&next=/settings/connections`}
                  >
                    Connect
                  </a>
                </Button>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

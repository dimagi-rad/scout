import { useCallback, useEffect, useState, type FormEvent } from "react"
import { BASE_PATH } from "@/config"
import { oauthConnectUrl, type OAuthProvider } from "@/lib/oauth"
import { useAppStore } from "@/store/store"
import { api } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

type Step = "choose" | "api-key"

interface MembershipResult {
  membership_id: string
  tenant_id: string
  tenant_name: string
}

interface ServerOption {
  value: string
  label: string
}

interface ApiKeyProviderSchema {
  id: string
  fields: { key: string; options?: ServerOption[] }[]
}

export function OnboardingWizard() {
  const [step, setStep] = useState<Step>("choose")
  const [server, setServer] = useState("")
  const [serverOptions, setServerOptions] = useState<ServerOption[]>([])
  const [domain, setDomain] = useState("")
  const [username, setUsername] = useState("")
  const [apiKey, setApiKey] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  // An OCS-only user lands here too, e.g. once their team-less memberships are
  // archived (#379), so the wizard must offer OCS or they have no way forward.
  const [ocs, setOcs] = useState<OAuthProvider | null>(null)
  // Listed only once its OAuth app is configured on this deployment (#719).
  const [commcareEu, setCommcareEu] = useState<OAuthProvider | null>(null)
  const [commcare, setCommcare] = useState<OAuthProvider | null>(null)
  const [providersState, setProvidersState] = useState<"loading" | "loaded" | "failed">(
    "loading",
  )

  const loadProviders = useCallback(async () => {
    setProvidersState("loading")
    try {
      const data = await api.get<{ providers: OAuthProvider[] }>("/api/auth/providers/")
      setOcs(data.providers.find((p) => p.id === "ocs") ?? null)
      setCommcareEu(data.providers.find((p) => p.id === "commcare_eu") ?? null)
      setCommcare(data.providers.find((p) => p.id === "commcare") ?? null)
      setProvidersState("loaded")
    } catch (err) {
      console.error("Failed to load sign-in options", err)
      setOcs(null)
      setCommcareEu(null)
      setCommcare(null)
      setProvidersState("failed")
    }
  }, [])

  useEffect(() => {
    void loadProviders()
  }, [loadProviders])

  // The server list comes from the API-key schema so it cannot drift from the backend.
  // Without it the key is checked against the default (www) server, as before.
  useEffect(() => {
    if (step !== "api-key") return
    api
      .get<ApiKeyProviderSchema[]>("/api/auth/api-key-providers/")
      .then((schemas) => {
        const commcare = schemas.find((s) => s.id === "commcare")
        setServerOptions(commcare?.fields.find((f) => f.key === "server")?.options ?? [])
      })
      .catch(() => setServerOptions([]))
  }, [step])

  async function handleApiKeySubmit(e: FormEvent) {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      await api.post<{ memberships: MembershipResult[] }>("/api/auth/connections/", {
        provider: "commcare",
        fields: {
          server,
          domain,
          username,
          api_key: apiKey,
        },
      })
      // Refresh auth state so onboarding_complete becomes true
      await fetchMe()
      await fetchDomains()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save credentials")
    } finally {
      setLoading(false)
    }
  }

  if (step === "api-key") {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-sm">
          <CardHeader>
            <CardTitle>Connect with API Key</CardTitle>
            <CardDescription>
              Find your API key in CommCare under Settings → My Account → API Key.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleApiKeySubmit} className="space-y-4">
              {serverOptions.length > 1 && (
                <div className="space-y-2">
                  <Label htmlFor="server">CommCare HQ server</Label>
                  <select
                    id="server"
                    data-testid="onboarding-server"
                    className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    value={server}
                    onChange={(e) => setServer(e.target.value)}
                  >
                    {serverOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div className="space-y-2">
                <Label htmlFor="domain">CommCare Domain</Label>
                <Input
                  id="domain"
                  data-testid="onboarding-domain"
                  required
                  placeholder="my-project"
                  value={domain}
                  onChange={(e) => setDomain(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="username">CommCare Username</Label>
                <Input
                  id="username"
                  data-testid="onboarding-username"
                  type="email"
                  required
                  placeholder="you@example.com"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="api-key">API Key</Label>
                <Input
                  id="api-key"
                  data-testid="onboarding-api-key"
                  type="password"
                  required
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                />
              </div>
              {error && <p className="text-sm text-destructive">{error}</p>}
              <div className="flex gap-2">
                <Button
                  type="button"
                  variant="outline"
                  className="flex-1"
                  onClick={() => setStep("choose")}
                >
                  Back
                </Button>
                <Button type="submit" className="flex-1" disabled={loading}>
                  {loading ? "Connecting..." : "Connect"}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          {/* Neutral until providers load, so the heading never flips from CommCare. */}
          <CardTitle>
            {ocs || providersState === "loading" ? "Connect your data" : "Connect your CommCare data"}
          </CardTitle>
          <CardDescription>
            {ocs || providersState === "loading"
              ? "Choose how to connect Scout to your data."
              : "Choose how to connect Scout to your CommCare account."}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {/* Until providers load, the www link is the historical default; once they
              have, it is offered only if this deployment configured it. */}
          {(commcare || providersState !== "loaded") && (
            <Button
              className="w-full"
              variant="outline"
              data-testid="onboarding-oauth"
              asChild
            >
              <a
                href={
                  commcare
                    ? oauthConnectUrl(commcare, "/")
                    : `${BASE_PATH}/accounts/commcare/login/?next=${BASE_PATH}/`
                }
              >
                {commcareEu ? "Connect with CommCare HQ (Global)" : "Connect with OAuth"}
              </a>
            </Button>
          )}
          {commcareEu && (
            <Button
              className="w-full"
              variant="outline"
              data-testid="onboarding-oauth-commcare-eu"
              asChild
            >
              <a href={oauthConnectUrl(commcareEu, "/")}>Connect with {commcareEu.name}</a>
            </Button>
          )}
          <Button
            className="w-full"
            data-testid="onboarding-api-key-option"
            onClick={() => setStep("api-key")}
          >
            Use an API Key
          </Button>
          {ocs && (
            <>
              {ocs.status === "needs_team" && (
                <p
                  className="text-sm text-amber-600"
                  data-testid="onboarding-ocs-needs-team"
                >
                  Your {ocs.name} sign-in has no team. Connect {ocs.name} and choose a team.
                </p>
              )}
              <Button className="w-full" variant="outline" data-testid="onboarding-ocs" asChild>
                <a href={oauthConnectUrl(ocs, "/")}>
                  Connect {ocs.name}
                </a>
              </Button>
            </>
          )}
          {providersState === "failed" && (
            <div className="space-y-2 text-center" data-testid="onboarding-providers-error">
              <p className="text-sm text-destructive">Couldn't load other sign-in options.</p>
              <Button variant="ghost" size="sm" onClick={() => void loadProviders()}>
                Try again
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

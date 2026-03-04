import { useState, type FormEvent } from "react"
import { useAppStore } from "@/store/store"
import { api } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

type Step = "choose" | "api-key"

export function OnboardingWizard() {
  const [step, setStep] = useState<Step>("choose")
  const [domain, setDomain] = useState("")
  const [username, setUsername] = useState("")
  const [apiKey, setApiKey] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)

  async function handleApiKeySubmit(e: FormEvent) {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      await api.post("/api/auth/tenant-credentials/", {
        provider: "commcare",
        tenant_id: domain,
        tenant_name: domain,
        credential: `${username}:${apiKey}`,
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

  // step === "choose"
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle>Connect your CommCare data</CardTitle>
          <CardDescription>
            Choose how to connect Scout to your CommCare account.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Button
            className="w-full"
            variant="outline"
            data-testid="onboarding-oauth"
            asChild
          >
            <a href="/accounts/commcare/login/?next=/">Connect with OAuth</a>
          </Button>
          <Button
            className="w-full"
            data-testid="onboarding-api-key-option"
            onClick={() => setStep("api-key")}
          >
            Use an API Key
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}

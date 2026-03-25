import { useState, useEffect, type FormEvent } from "react"
import { useAppStore } from "@/store/store"
import { api } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { useEmbedParams } from "@/hooks/useEmbedParams"
import { BASE_PATH } from "@/config"

interface OAuthProvider {
  id: string
  name: string
  login_url: string
}

export function LoginForm() {
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [loading, setLoading] = useState(false)
  const [providers, setProviders] = useState<OAuthProvider[]>([])
  const authError = useAppStore((s) => s.authError)
  const login = useAppStore((s) => s.authActions.login)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const { isEmbed } = useEmbedParams()

  useEffect(() => {
    api.get<{ providers: OAuthProvider[] }>("/api/auth/providers/")
      .then((data) => setProviders(data.providers))
      .catch(() => {})
  }, [])

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setLoading(true)
    try {
      await login(email, password)
    } catch {
      // error is set in the store
    } finally {
      setLoading(false)
    }
  }

  function handleOAuthClick(e: React.MouseEvent, provider: OAuthProvider) {
    if (!isEmbed) return // Let the <a> navigate normally

    e.preventDefault()
    const nextUrl = `${BASE_PATH}/embed/?popup_close=1`
    const authUrl = `${provider.login_url}?next=${encodeURIComponent(nextUrl)}`
    const popup = window.open(authUrl, "scout-oauth", "width=500,height=700")

    if (!popup) return

    // Poll for popup close — when it closes, re-fetch auth status
    const interval = setInterval(() => {
      if (!popup || popup.closed) {
        clearInterval(interval)
        fetchMe()
      }
    }, 500)
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle className="text-2xl">Scout</CardTitle>
          <CardDescription>Sign in to your account</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                required
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            {authError && (
              <p className="text-sm text-destructive">{authError}</p>
            )}
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? "Signing in..." : "Sign in"}
            </Button>
          </form>
          {providers.length > 0 && (
            <>
              <div className="relative my-4">
                <div className="absolute inset-0 flex items-center">
                  <span className="w-full border-t" />
                </div>
                <div className="relative flex justify-center text-xs uppercase">
                  <span className="bg-card px-2 text-muted-foreground">
                    or continue with
                  </span>
                </div>
              </div>
              <div className="space-y-2">
                {providers.map((provider) => (
                  <Button
                    key={provider.id}
                    variant="outline"
                    className="w-full"
                    asChild
                    data-testid={`oauth-login-${provider.id}`}
                  >
                    <a
                      href={`${BASE_PATH}${provider.login_url}?next=${BASE_PATH}/`}
                      onClick={(e) => handleOAuthClick(e, provider)}
                    >
                      {provider.name}
                    </a>
                  </Button>
                ))}
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

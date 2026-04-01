import { useState, useEffect, useRef, type FormEvent } from "react"
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
  const [popupProvider, setPopupProvider] = useState<string | null>(null)
  const authError = useAppStore((s) => s.authError)
  const login = useAppStore((s) => s.authActions.login)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const { isEmbed } = useEmbedParams()
  const popupRef = useRef<Window | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    api.get<{ providers: OAuthProvider[] }>("/api/auth/providers/")
      .then((data) => setProviders(data.providers))
      .catch(() => {})
  }, [])

  // Listen for postMessage from the popup (token relay)
  useEffect(() => {
    if (!isEmbed) return

    function handleMessage(event: MessageEvent) {
      if (event.origin !== window.location.origin) return
      if (event.data?.type !== "scout:auth-token") return

      const token = event.data.token
      if (!token) return

      // Exchange the token for a session cookie in this iframe's context
      api.post("/api/auth/token-exchange/", { token })
        .then(() => fetchMe())
        .catch(() => {
          // Token exchange failed, reset to login form
          setPopupProvider(null)
        })
    }

    window.addEventListener("message", handleMessage)
    return () => window.removeEventListener("message", handleMessage)
  }, [isEmbed, fetchMe])

  // Clean up polling on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
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

  function openLoginPopup(providerId: string) {
    // Open popup directly to the OAuth provider (LOGIN_ON_GET=True skips
    // the intermediate page). After OAuth, the callback redirects to
    // /auth/popup-complete/ which sends the token via postMessage.
    const callbackUrl = `${BASE_PATH}/auth/popup-complete/`
    const loginUrl = `${BASE_PATH}/accounts/${providerId}/login/?next=${encodeURIComponent(callbackUrl)}`
    const popup = window.open(loginUrl, "scout-oauth", "width=500,height=700")
    popupRef.current = popup

    if (!popup) {
      // Popup was blocked
      setPopupProvider(null)
      return
    }

    setPopupProvider(providerId)

    // Fallback: poll for popup.closed in case postMessage doesn't arrive
    // (e.g. user closes popup manually)
    pollRef.current = setInterval(() => {
      if (!popup || popup.closed) {
        if (pollRef.current) clearInterval(pollRef.current)
        pollRef.current = null
        popupRef.current = null
        setPopupProvider(null)
      }
    }, 500)
  }

  function cancelPopup() {
    if (popupRef.current && !popupRef.current.closed) {
      popupRef.current.close()
    }
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = null
    popupRef.current = null
    setPopupProvider(null)
  }

  // Show a "signing in" state while the popup is open
  if (popupProvider) {
    const providerName = providers.find((p) => p.id === popupProvider)?.name ?? popupProvider
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-sm">
          <CardHeader className="text-center">
            <CardTitle className="text-2xl">Scout</CardTitle>
            <CardDescription>Signing in via {providerName}...</CardDescription>
          </CardHeader>
          <CardContent className="text-center space-y-4">
            <div className="flex justify-center">
              <div className="h-8 w-8 animate-spin rounded-full border-4 border-muted border-t-primary" />
            </div>
            <p className="text-sm text-muted-foreground">
              Complete the login in the popup window.
            </p>
            <Button variant="ghost" size="sm" onClick={cancelPopup}>
              Cancel
            </Button>
          </CardContent>
        </Card>
      </div>
    )
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
                  isEmbed ? (
                    <Button
                      key={provider.id}
                      variant="outline"
                      className="w-full"
                      data-testid={`oauth-login-${provider.id}`}
                      onClick={() => openLoginPopup(provider.id)}
                    >
                      {provider.name}
                    </Button>
                  ) : (
                    <Button
                      key={provider.id}
                      variant="outline"
                      className="w-full"
                      asChild
                      data-testid={`oauth-login-${provider.id}`}
                    >
                      <a href={`${BASE_PATH}${provider.login_url}?next=${BASE_PATH}/`}>
                        {provider.name}
                      </a>
                    </Button>
                  )
                ))}
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

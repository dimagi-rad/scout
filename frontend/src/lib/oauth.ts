import type { MouseEvent } from "react"

import { api, getCsrfToken } from "@/api/client"
import { BASE_PATH } from "@/config"

/**
 * needs_team: a team-less OCS sign-in that can reach no data (#379).
 * unavailable: a refresh couldn't run right now; the credential isn't known to be dead (#779).
 */
export type OAuthProviderStatus =
  | "connected"
  | "unavailable"
  | "expired"
  | "needs_team"
  | "disconnected"

/** One entry of GET /api/auth/providers/. */
export interface OAuthProvider {
  id: string
  name: string
  login_url: string
  connected?: boolean
  status?: OAuthProviderStatus | null
  /** True when one token covers one scope (an OCS team), so several can coexist. */
  supports_multiple_scopes?: boolean
  /** The user's OAuth connections this card covers (ids from /api/auth/connections/). */
  connection_ids?: string[]
}

/** Start OAuth for an already signed-in user, returning to the app path `next`. */
export function oauthConnectUrl(provider: OAuthProvider, next: string): string {
  const redirect = encodeURIComponent(`${BASE_PATH}${next}`)
  return `${BASE_PATH}${provider.login_url}?process=connect&next=${redirect}`
}

/**
 * Submit an allauth provider login URL as a CSRF-token POST. allauth only starts OAuth
 * on POST (SOCIALACCOUNT_LOGIN_ON_GET=False, arch #258) and answers a GET with its own
 * "Continue" page, so posting from our UI goes straight to the provider.
 */
export async function postOAuthStart(href: string, target = ""): Promise<void> {
  let token = getCsrfToken()
  if (!token) {
    try {
      await api.get("/api/auth/csrf/")
    } catch {
      // Without a token the GET interstitial below still works, just with an extra click.
    }
    token = getCsrfToken()
  }
  if (!token) {
    window.open(href, target || "_self")
    return
  }
  const url = new URL(href, window.location.href)
  const form = document.createElement("form")
  form.method = "post"
  form.action = `${url.origin}${url.pathname}`
  if (target) form.target = target
  for (const [name, value] of [...url.searchParams, ["csrfmiddlewaretoken", token]]) {
    const input = document.createElement("input")
    input.type = "hidden"
    input.name = name
    input.value = value
    form.appendChild(input)
  }
  document.body.appendChild(form)
  form.submit()
}

/**
 * onClick for an <a> whose href is a provider login URL. A plain click POSTs; modified
 * clicks (new tab, etc.) keep the href's GET, which lands on allauth's confirmation page.
 */
export function startOAuthOnClick(event: MouseEvent<HTMLAnchorElement>): void {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  ) {
    return
  }
  event.preventDefault()
  void postOAuthStart(event.currentTarget.href, event.currentTarget.target)
}

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
}

/** Start OAuth for an already signed-in user, returning to the app path `next`. */
export function oauthConnectUrl(provider: OAuthProvider, next: string): string {
  const redirect = encodeURIComponent(`${BASE_PATH}${next}`)
  return `${BASE_PATH}${provider.login_url}?process=connect&next=${redirect}`
}

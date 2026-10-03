import type { ApiKeyConnection } from "@/components/ApiConnectionDialog"
import { productName, sourceCount, sourcesNoun } from "./accessCopy"

function liveSources(connections: ApiKeyConnection[]): number {
  return connections.reduce((n, c) => n + c.chatbots.length, 0)
}

/** "all 12 bots", but "1 bot", and "any bots" when there are none. */
function allOf(provider: string, n: number): string {
  if (n === 0) return `any ${sourcesNoun(provider)}`
  return n === 1 ? sourceCount(provider, n) : `all ${sourceCount(provider, n)}`
}

/** The provider card's button: a team-scoped provider signs out of every team at once. */
export function providerDisconnectLabel(supportsMultipleScopes: boolean | undefined): string {
  return supportsMultipleScopes ? "Disconnect all teams" : "Disconnect"
}

/**
 * What disconnecting a provider removes. Mirrors disconnect_provider_view: it deletes
 * every OAuth sign-in for the provider (one HQ server for CommCare) and archives
 * their sources; API key connections are left alone.
 */
export function providerDisconnectMessage(
  providerName: string,
  dataProvider: string,
  supportsMultipleScopes: boolean | undefined,
  oauthConnections: { conn: ApiKeyConnection; teamLabel: string }[],
  hasApiKeys: boolean,
): string {
  const total = liveSources(oauthConnections.map((c) => c.conn))
  const sources = sourceCount(dataProvider, total)
  const keys = hasApiKeys ? " API key connections stay." : ""
  if (supportsMultipleScopes) {
    const n = oauthConnections.length
    const teams = oauthConnections.map((c) => c.teamLabel).join(", ")
    return (
      `Disconnect all ${n} ${productName(dataProvider)} ${n === 1 ? "team" : "teams"}` +
      `${teams ? ` (${teams})` : ""}? Scout loses access to their ${sources}.${keys}` +
      " You can reconnect later."
    )
  }
  return (
    `Disconnect ${providerName}? It is one sign-in for all your ${sourcesNoun(dataProvider)}, ` +
    `so Scout loses access to ${allOf(dataProvider, total)}.${keys} You can reconnect later.`
  )
}

/** What removing one connection (DELETE /api/auth/connections/<id>/) removes. */
export function connectionRemoveCopy(
  conn: ApiKeyConnection,
  teamLabel: string,
): { action: string; message: string } {
  const sources = sourceCount(conn.provider, conn.chatbots.length)
  if (conn.credential_type === "api_key") {
    return {
      action: "Remove",
      message:
        `Remove the API key connection ${teamLabel}? Scout loses access to its ${sources}` +
        " and deletes the saved key. You can add it again later.",
    }
  }
  if (conn.provider === "ocs") {
    return {
      action: "Disconnect",
      message:
        `Disconnect team ${teamLabel}? Scout loses access to its ${sources}.` +
        " Your other teams stay connected. You can reconnect later.",
    }
  }
  return {
    action: "Disconnect",
    message: `Disconnect ${teamLabel}? Scout loses access to ${allOf(conn.provider, conn.chatbots.length)}. You can reconnect later.`,
  }
}

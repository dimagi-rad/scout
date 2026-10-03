import type { ApiKeyConnection } from "@/components/ApiConnectionDialog"

const PRODUCT: Record<string, string> = {
  commcare: "CommCare",
  commcare_connect: "CommCare Connect",
  ocs: "Open Chat Studio",
}

const SOURCE_NOUN: Record<string, string> = {
  commcare: "projects",
  commcare_connect: "opportunities",
  ocs: "bots",
}

export function productName(provider: string): string {
  return PRODUCT[provider] ?? provider
}

function withArticle(word: string): string {
  return `${/^[aeiou]/i.test(word) ? "an" : "a"} ${word}`
}

/** "team dimagi-dev" for a team-scoped connection, else the card's own label. */
export function scopePhrase(conn: ApiKeyConnection, teamLabel: string): string {
  return conn.provider === "ocs" ? `team ${teamLabel}` : teamLabel
}

export interface AccessNotice {
  title: string
  body: string
  /** Reconnecting can help: offer the button. */
  offerReconnect: boolean
}

/** What to tell the user about a connection that no longer reaches its sources. */
export function accessNotice(conn: ApiKeyConnection, teamLabel: string): AccessNotice | null {
  const product = productName(conn.provider)
  const noun = SOURCE_NOUN[conn.provider] ?? "sources"
  const isTeam = conn.provider === "ocs"
  const isApiKey = conn.credential_type === "api_key"
  const scope = scopePhrase(conn, teamLabel)
  const admin = isTeam
    ? `Ask ${withArticle(product)} admin for the team to restore your access`
    : `Ask ${withArticle(product)} admin to restore your access`
  const retry = isApiKey
    ? "then click Refresh sources."
    : `then click Refresh sources, or reconnect${isTeam ? " choosing this team" : ""}.`

  switch (conn.access_state) {
    case "expired":
      return isApiKey
        ? {
            title: `${product} rejected the API key for ${scope}.`,
            body: "Edit the connection and enter a current key.",
            offerReconnect: false,
          }
        : {
            title: `Your ${product} sign-in for ${scope} has expired.`,
            body: "Reconnect to restore access.",
            offerReconnect: true,
          }
    case "refused":
      return {
        title: `${product} isn't granting access for ${scope}.`,
        body: isTeam
          ? `You may have been removed from this team in ${product}, or lost access to its ${noun}. ${admin}, ${retry}`
          : `You may have lost access in ${product}. ${admin}, ${retry}`,
        offerReconnect: !isApiKey,
      }
    case "partial": {
      const lost = (conn.archived_chatbots ?? []).filter((b) => b.archived_reason !== "unlisted").length
      const total = lost + conn.chatbots.length
      return {
        title: `${product} isn't granting access to ${lost} of ${total} ${noun} for ${scope}.`,
        body: `They're marked No access below. If you should still have them, ${admin.charAt(0).toLowerCase()}${admin.slice(1)}, then click Refresh sources.`,
        offerReconnect: false,
      }
    }
    default:
      return null
  }
}

/** One line per kind of problem among a provider card's connections. */
export function providerAccessLines(
  providerName: string,
  connections: { conn: ApiKeyConnection; teamLabel: string }[],
): string[] {
  const refused = connections
    .filter(({ conn }) => conn.access_state === "refused" || conn.access_state === "partial")
    .map(({ conn, teamLabel }) => scopePhrase(conn, teamLabel))
  const expired = connections
    .filter(({ conn }) => conn.access_state === "expired")
    .map(({ conn, teamLabel }) => scopePhrase(conn, teamLabel))
  const lines: string[] = []
  if (refused.length) {
    lines.push(`${providerName} isn't granting access for ${refused.join(", ")}.`)
  }
  if (expired.length) lines.push(`Sign-in expired for ${expired.join(", ")}.`)
  return lines
}

import { ChevronRight } from "lucide-react"
import type { ReactNode } from "react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import type { ApiKeyConnection } from "@/components/ApiConnectionDialog"
import { FALLBACK_TINT, PROVIDER_TINT } from "@/components/WorkspaceBadge/providerMeta"
import { cn } from "@/lib/utils"
import { productName, sourceCount, type AccessNotice } from "./accessCopy"

export const WARNING_BADGE =
  "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400"

interface ConnectionCardProps {
  conn: ApiKeyConnection
  teamLabel: string
  statusBadge?: { label: string; testId: string }
  notice: AccessNotice | null
  expanded: boolean
  onToggle: () => void
  /** Edit / Disconnect buttons, or nothing while a confirmation is open. */
  actions: ReactNode
  /** The Reconnect button for the notice, when one applies. */
  reconnect: ReactNode
  /** The in-card removal confirmation, when open. */
  confirmation: ReactNode
}

/** A connection, collapsed to one header line until opened to list its sources. */
export function ConnectionCard({
  conn,
  teamLabel,
  statusBadge,
  notice,
  expanded,
  onToggle,
  actions,
  reconnect,
  confirmation,
}: ConnectionCardProps) {
  const isApiKey = conn.credential_type === "api_key"
  const archived = conn.archived_chatbots ?? []
  const denied = archived.filter((cb) => cb.archived_reason !== "unlisted").length
  const id = conn.connection_id
  const sourcesId = `connection-sources-${id}`

  return (
    <Card data-testid={`connection-card-${id}`}>
      <CardContent className="space-y-3 p-3">
        <div className="flex items-start justify-between gap-4">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={expanded}
            aria-controls={sourcesId}
            data-testid={`connection-toggle-${id}`}
            className="flex min-w-0 flex-1 items-start gap-2 rounded-md text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <ChevronRight
              className={cn(
                "mt-0.5 h-4 w-4 shrink-0 text-muted-foreground transition-transform",
                expanded && "rotate-90",
              )}
              aria-hidden
            />
            <span className="min-w-0 space-y-1">
              <span className="block font-medium" data-testid={`connection-team-${id}`}>
                {teamLabel}
              </span>
              <span className="flex flex-wrap items-center gap-2">
                <Badge variant="secondary" className={PROVIDER_TINT[conn.provider] ?? FALLBACK_TINT}>
                  {productName(conn.provider)}
                </Badge>
                <Badge variant="secondary">{isApiKey ? "API Key" : "OAuth"}</Badge>
                <span
                  className="text-xs text-muted-foreground"
                  data-testid={`connection-source-count-${id}`}
                >
                  {sourceCount(conn.provider, conn.chatbots.length)}
                  {denied > 0 && ` · ${denied} no access`}
                </span>
                {statusBadge && (
                  <Badge
                    variant="secondary"
                    className={WARNING_BADGE}
                    data-testid={`${statusBadge.testId}-${id}`}
                  >
                    {statusBadge.label}
                  </Badge>
                )}
              </span>
            </span>
          </button>
          {actions && <div className="flex shrink-0 gap-2">{actions}</div>}
        </div>

        {notice && (
          <div
            className="space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-300"
            data-testid={`connection-access-${id}`}
            data-access-state={conn.access_state}
          >
            <p className="font-medium">{notice.title}</p>
            <p>{notice.body}</p>
            {reconnect}
          </div>
        )}

        {confirmation}

        {expanded && (
          <ul id={sourcesId} className="space-y-1 border-t pt-3" data-testid={sourcesId}>
            {conn.chatbots.length === 0 && archived.length === 0 && (
              <li className="text-sm text-muted-foreground">No sources.</li>
            )}
            {conn.chatbots.map((cb) => (
              <li key={cb.membership_id} className="flex items-center justify-between gap-4 text-sm">
                <span className="font-medium">{cb.tenant_name || cb.tenant_id}</span>
                <span className="text-muted-foreground">{cb.tenant_id}</span>
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
        )}
      </CardContent>
    </Card>
  )
}

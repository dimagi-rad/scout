import { useMemo } from "react"
import { useNavigate } from "react-router-dom"
import { AlertTriangle } from "lucide-react"
import { useAppStore } from "@/store/store"
import { workspaceHasAccess } from "@/api/workspaces"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"
import { recordWorkspaceUse } from "@/lib/recentWorkspaces"
import { workspacePath } from "@/lib/workspacePath"

/** Distinct provider labels for a workspace, e.g. "CommCare" or "CommCare, Open Chat Studio". */
function providerLabels(tenants: { provider: string }[]): string {
  const labels = [...new Set(tenants.map((t) => getProviderMeta(t.provider).label))]
  return labels.join(", ")
}

/**
 * A hard, non-dismissible gate shown when the active workspace is one the user
 * has lost upstream access to. The backend already refuses its data (403), so
 * the page behind is dead; this makes that legible and the only way out is to
 * pick a workspace the user can still access. Reachable only via a stale
 * default or a deep link — the switcher and default-pick avoid orphans.
 */
export function LostAccessModal() {
  const navigate = useNavigate()
  const domains = useAppStore((s) => s.domains)
  const domainsStatus = useAppStore((s) => s.domainsStatus)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const newThread = useAppStore((s) => s.uiActions.newThread)

  const active = domains.find((d) => d.id === activeDomainId)
  const accessible = useMemo(() => domains.filter(workspaceHasAccess), [domains])

  // Only gate once the list has actually loaded and resolved to an orphan —
  // never during the initial load, or we'd flash the modal before we know.
  if (domainsStatus !== "loaded" || !active || workspaceHasAccess(active)) return null

  const source = providerLabels(active.tenants ?? [])

  function goTo(ws: (typeof domains)[number]) {
    recordWorkspaceUse(ws.id)
    setActiveDomain(ws.id)
    newThread()
    navigate(`${workspacePath(ws)}/chat`)
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="lost-access-title"
      data-testid="lost-access-modal"
      className="fixed inset-0 z-[100] flex items-center justify-center bg-background/70 p-4 backdrop-blur-sm"
    >
      <div className="w-full max-w-md rounded-lg border bg-card p-6 shadow-xl">
        <div className="mb-4 flex items-center gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-amber-100 text-amber-600 dark:bg-amber-950 dark:text-amber-400">
            <AlertTriangle className="h-5 w-5" aria-hidden />
          </span>
          <h2 id="lost-access-title" className="text-lg font-semibold">
            You’ve lost access to “{active.display_name}”
          </h2>
        </div>

        <p className="text-sm text-muted-foreground">
          {source ? (
            <>
              This is a <span className="font-medium text-foreground">{source}</span> workspace.
              Your access appears to have been removed upstream. Check your access on {source}, or
              if you think this is a mistake, reach out to the workspace owner or an admin.
            </>
          ) : (
            <>
              Your access to this workspace appears to have been removed upstream. If you think
              this is a mistake, reach out to the workspace owner or an admin.
            </>
          )}
        </p>

        {accessible.length > 0 ? (
          <div className="mt-5">
            <p className="mb-2 text-sm font-medium">Go to a workspace you can access:</p>
            <div className="max-h-56 space-y-1 overflow-y-auto" data-testid="lost-access-picker">
              {accessible.map((ws) => {
                const { Icon } = getProviderMeta(ws.tenants?.[0]?.provider)
                return (
                  <button
                    key={ws.id}
                    data-testid={`lost-access-goto-${ws.id}`}
                    onClick={() => goTo(ws)}
                    className="flex w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-sm transition-colors hover:bg-accent hover:text-accent-foreground"
                  >
                    <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                    <span className="truncate">{ws.display_name}</span>
                  </button>
                )
              })}
            </div>
          </div>
        ) : (
          <p className="mt-5 rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
            You don’t have access to any workspaces right now. Reconnect your account or ask an
            admin to restore access.
          </p>
        )}
      </div>
    </div>
  )
}

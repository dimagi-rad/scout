import { useMemo, useState } from "react"
import { useLocation, useNavigate } from "react-router-dom"
import { AlertTriangle } from "lucide-react"
import { useAppStore } from "@/store/store"
import { workspaceHasAccess } from "@/api/workspaces"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"
import { workspacePath } from "@/lib/workspacePath"
import { CONNECTIONS_PATH } from "@/lib/routes"

/** Distinct provider labels for a workspace, e.g. "CommCare" or "CommCare, Open Chat Studio". */
function providerLabels(tenants: { provider: string }[]): string {
  const labels = [...new Set(tenants.map((t) => getProviderMeta(t.provider).label))]
  return labels.join(", ")
}

/**
 * A hard, non-dismissible gate shown when the active workspace needs a source
 * the user cannot use. The backend already refuses its data (403), so the page
 * behind is dead; this links to Connected Accounts, where the user can connect
 * or reconnect what is missing (naming each source when the server lists them,
 * #380), or lets the user pick a workspace they can still access. Reachable only via a stale
 * default or a deep link — the switcher and default-pick avoid orphans.
 */
export function LostAccessModal() {
  const navigate = useNavigate()
  const location = useLocation()
  const domains = useAppStore((s) => s.domains)
  const domainsStatus = useAppStore((s) => s.domainsStatus)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const newThread = useAppStore((s) => s.uiActions.newThread)
  const retryAccessVerification = useAppStore((s) => s.uiActions.retryAccessVerification)
  const retryOutcome = useAppStore((s) => s.threadsAccessLostMessage)
  const [verifyingId, setVerifyingId] = useState<string | null>(null)

  const active = domains.find((d) => d.id === activeDomainId)
  const accessible = useMemo(() => domains.filter(workspaceHasAccess), [domains])

  // Connected Accounts and the workspace's own page (remove a source, leave,
  // delete) are where the user fixes this, so the gate must not cover them.
  const path = location.pathname.replace(/\/+$/, "")
  const onRecoveryPage =
    path.endsWith(CONNECTIONS_PATH) ||
    (activeDomainId !== null && path.startsWith("/workspaces/") && path.endsWith(`/${activeDomainId}`))

  // Only gate once the list has actually loaded and resolved to an orphan —
  // never during the initial load, or we'd flash the modal before we know.
  if (domainsStatus !== "loaded" || !active || workspaceHasAccess(active) || onRecoveryPage) {
    return null
  }

  const source = providerLabels(active.tenants ?? [])
  const missing = active.missing_tenants ?? []

  function goTo(ws: (typeof domains)[number]) {
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
            {missing.length > 0
              ? `You can’t open “${active.display_name}” yet`
              : `You’ve lost access to “${active.display_name}”`}
          </h2>
        </div>

        {missing.length > 0 ? (
          <div className="text-sm text-muted-foreground">
            <p>This workspace needs access to every one of its data sources. Still needed:</p>
            <ul className="mt-2 space-y-1" data-testid="lost-access-missing">
              {missing.map((t) => (
                <li key={t.tenant_id} data-testid={`lost-access-missing-${t.tenant_id}`}>
                  <span className="font-medium text-foreground">{t.tenant_name}</span>: {t.remedy}
                </li>
              ))}
            </ul>
            <p className="mt-2">Access returns automatically once that is fixed.</p>
          </div>
        ) : (
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
        )}

        {retryOutcome && (
          <p className="mt-3 text-sm text-muted-foreground" data-testid="lost-access-retry-outcome">
            {retryOutcome}
          </p>
        )}

        <div className="mt-3 flex flex-wrap gap-2">
          <button
            data-testid="lost-access-connections"
            onClick={() => navigate(CONNECTIONS_PATH)}
            className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Open Connected Accounts
          </button>
          <button
            type="button"
            disabled={verifyingId === active.id}
            onClick={() => {
              const workspaceId = active.id
              setVerifyingId(workspaceId)
              void retryAccessVerification(workspaceId).finally(() =>
                setVerifyingId((current) => (current === workspaceId ? null : current)),
              )
            }}
            className="rounded-md border px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground disabled:opacity-50"
            data-testid="lost-access-retry-verification"
          >
            {verifyingId === active.id ? "Verifying…" : "Retry verification"}
          </button>
          <button
            data-testid="lost-access-workspace-settings"
            onClick={() => navigate(workspacePath(active))}
            className="rounded-md border px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
          >
            Leave or edit this workspace
          </button>
        </div>

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

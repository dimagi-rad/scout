import { AlertTriangle, X } from "lucide-react"
import { Link } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { CONNECTIONS_PATH } from "@/lib/routes"
import { useAppStore } from "@/store/store"

interface Props {
  // Onboarding renders outside the router and already offers "Connect Open Chat Studio".
  showConnectionsLink?: boolean
}

export function OcsAccessNotice({ showConnectionsLink = false }: Props) {
  const denied = useAppStore((s) => s.user?.ocs_access_denied)
  const dismiss = useAppStore((s) => s.authActions.dismissOcsAccessNotice)
  if (!denied?.teams?.length) return null
  const named = denied.teams.filter((t) => t.slug).map((t) => t.name)

  return (
    <div
      className="flex items-start gap-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 shadow-sm dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100"
      role="alert"
      data-testid="ocs-access-notice"
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="font-medium">Open Chat Studio didn't share your chatbots</p>
        <p>
          Open Chat Studio refused Scout access to{" "}
          {named.length ? (
            <>
              the chatbots of {named.length === 1 ? "team" : "teams"}{" "}
              <span className="font-medium">{named.join(", ")}</span>
            </>
          ) : (
            "your chatbots"
          )}
          . Ask a team admin to check your permissions, or connect a different team.
        </p>
        {showConnectionsLink && (
          <Link
            to={CONNECTIONS_PATH}
            className="font-medium underline underline-offset-2"
            data-testid="ocs-access-notice-connections"
          >
            Manage connected accounts
          </Link>
        )}
      </div>
      <Button
        variant="ghost"
        size="icon"
        className="h-6 w-6 shrink-0"
        aria-label="Dismiss Open Chat Studio notice"
        onClick={() => void dismiss()}
        data-testid="ocs-access-notice-dismiss"
      >
        <X className="h-3.5 w-3.5" />
      </Button>
    </div>
  )
}

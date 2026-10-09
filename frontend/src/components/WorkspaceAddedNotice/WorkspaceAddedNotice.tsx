import { useState } from "react"
import { Users, X } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { workspacePath } from "@/lib/workspacePath"
import { useAppStore } from "@/store/store"

// More than a few stacked notices bury the page, so the rest wait behind "Show more".
const MAX_VISIBLE = 3

export function WorkspaceAddedNotice() {
  const navigate = useNavigate()
  const domains = useAppStore((s) => s.domains)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const addedDomainIds = useAppStore((s) => s.addedDomainIds)
  const dismissAddedDomain = useAppStore((s) => s.domainActions.dismissAddedDomain)
  const dismissAllAddedDomains = useAppStore((s) => s.domainActions.dismissAllAddedDomains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const newThread = useAppStore((s) => s.uiActions.newThread)

  const added = addedDomainIds
    .filter((id) => id !== activeDomainId)
    .map((id) => domains.find((d) => d.id === id))
    .filter((d) => d !== undefined)
  const [expanded, setExpanded] = useState(false)
  const visible = expanded ? added : added.slice(0, MAX_VISIBLE)
  const hiddenCount = added.length - visible.length

  // Stays mounted while empty: screen readers skip a live region inserted along with its content.
  // Sits above the full-width OfflineBanner (z-50) rather than over it, so neither hides the other.
  return (
    <div
      className="fixed bottom-16 right-4 z-40 flex max-h-[calc(100vh-6rem)] w-80 max-w-[calc(100vw-2rem)] flex-col gap-2 overflow-y-auto"
      role="status"
      aria-live="polite"
      data-testid="workspace-added-notice"
    >
      {visible.map((workspace) => (
        <div
          key={workspace.id}
          className="flex items-start gap-3 rounded-lg border bg-card p-3 text-sm shadow-lg"
          data-testid={`workspace-added-notice-${workspace.id}`}
        >
          <Users className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
          <div className="min-w-0 flex-1">
            <p>
              You now have access to{" "}
              <span className="font-medium">{workspace.display_name}</span>.
            </p>
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0"
              onClick={() => {
                setActiveDomain(workspace.id)
                newThread()
                navigate(`${workspacePath(workspace)}/chat`)
              }}
              data-testid={`workspace-added-notice-open-${workspace.id}`}
            >
              Open workspace
            </Button>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 shrink-0"
            aria-label={`Dismiss notice for ${workspace.display_name}`}
            onClick={() => dismissAddedDomain(workspace.id)}
            data-testid={`workspace-added-notice-dismiss-${workspace.id}`}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      ))}
      {added.length > 1 && (
        <div className="flex items-center justify-between gap-3 rounded-lg border bg-card px-3 py-2 text-sm shadow-lg">
          {hiddenCount > 0 ? (
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0"
              onClick={() => setExpanded(true)}
              data-testid="workspace-added-notice-show-more"
            >
              Show {hiddenCount} more
            </Button>
          ) : (
            <span />
          )}
          <Button
            variant="ghost"
            size="sm"
            className="h-7"
            onClick={() => {
              setExpanded(false)
              dismissAllAddedDomains()
            }}
            data-testid="workspace-added-notice-dismiss-all"
          >
            Dismiss all
          </Button>
        </div>
      )}
    </div>
  )
}

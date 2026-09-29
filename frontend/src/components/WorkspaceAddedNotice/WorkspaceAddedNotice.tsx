import { Users, X } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { workspacePath } from "@/lib/workspacePath"
import { useAppStore } from "@/store/store"

export function WorkspaceAddedNotice() {
  const navigate = useNavigate()
  const domains = useAppStore((s) => s.domains)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const addedDomainIds = useAppStore((s) => s.addedDomainIds)
  const dismissAddedDomain = useAppStore((s) => s.domainActions.dismissAddedDomain)

  const added = addedDomainIds
    .filter((id) => id !== activeDomainId)
    .map((id) => domains.find((d) => d.id === id))
    .filter((d) => d !== undefined)

  if (added.length === 0) return null

  return (
    <div
      className="fixed bottom-4 right-4 z-40 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2"
      role="status"
      aria-live="polite"
      data-testid="workspace-added-notice"
    >
      {added.map((workspace) => (
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
                dismissAddedDomain(workspace.id)
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
    </div>
  )
}

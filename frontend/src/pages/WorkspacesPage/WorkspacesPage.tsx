import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { TenantMembership } from "@/store/domainSlice"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal"
import { Button } from "@/components/ui/button"
import { Users, Database, ChevronRight } from "lucide-react"

function roleBadge(role: string) {
  const styles: Record<string, string> = {
    manage: "bg-green-950 text-green-400",
    read_write: "bg-blue-950 text-blue-400",
    read: "bg-muted text-muted-foreground",
  }
  const labels: Record<string, string> = {
    manage: "Manager",
    read_write: "Read-Write",
    read: "Read",
  }
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${styles[role] ?? styles.read}`}
    >
      {labels[role] ?? role}
    </span>
  )
}

function WorkspaceRow({ workspace, onClick }: { workspace: TenantMembership; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      data-testid={`workspace-row-${workspace.id}`}
      className="flex w-full items-center justify-between rounded-lg border bg-card px-4 py-3 text-left transition-colors hover:bg-accent"
    >
      <div>
        <div className="font-medium">{workspace.name}</div>
        <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <Users className="h-3 w-3" />
            {workspace.member_count} {workspace.member_count === 1 ? "member" : "members"}
          </span>
          <span className="flex items-center gap-1">
            <Database className="h-3 w-3" />
            {workspace.tenant_count} {workspace.tenant_count === 1 ? "source" : "sources"}
          </span>
        </div>
      </div>
      <div className="flex items-center gap-3">
        {roleBadge(workspace.role)}
        <ChevronRight className="h-4 w-4 text-muted-foreground" />
      </div>
    </button>
  )
}

export function WorkspacesPage() {
  const navigate = useNavigate()
  const domains = useAppStore((s) => s.domains)
  const domainsStatus = useAppStore((s) => s.domainsStatus)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const [showCreate, setShowCreate] = useState(false)

  const isLoading = domainsStatus === "loading"

  return (
    <div className="mx-auto max-w-2xl px-6 py-8">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold" data-testid="workspaces-title">Workspaces</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Your workspaces across connected data sources
          </p>
        </div>
        <Button onClick={() => setShowCreate(true)} data-testid="new-workspace-btn">
          New workspace
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-16 animate-pulse rounded-lg border bg-muted" />
          ))}
        </div>
      ) : domains.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center">
          <p className="text-muted-foreground">No workspaces yet.</p>
          <Button className="mt-4" onClick={() => setShowCreate(true)}>
            Create your first workspace
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          {domains.map((ws) => (
            <WorkspaceRow
              key={ws.id}
              workspace={ws}
              onClick={() => navigate(`/workspaces/${ws.id}`)}
            />
          ))}
        </div>
      )}

      {showCreate && (
        <CreateWorkspaceModal
          onClose={() => {
            setShowCreate(false)
            fetchDomains()
          }}
        />
      )}
    </div>
  )
}

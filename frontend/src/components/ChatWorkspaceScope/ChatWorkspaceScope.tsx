import { useLocation, useNavigate } from "react-router-dom"
import { ChevronRight } from "lucide-react"
import { useAppStore } from "@/store/store"
import { FALLBACK_TINT, getProviderMeta, PROVIDER_TINT } from "@/components/WorkspaceBadge/providerMeta"
import { workspacePath } from "@/lib/workspacePath"
import { cn } from "@/lib/utils"

const MAX_CHIPS = 3

/**
 * Names the workspace the chat answers from and the sources in it, so a wrong
 * workspace is visible before the question is asked (#916).
 */
export function ChatWorkspaceScope() {
  const workspace = useAppStore((s) => s.domains.find((d) => d.id === s.activeDomainId))
  const navigate = useNavigate()
  const pathPrefix = useLocation().pathname.startsWith("/embed") ? "/embed" : ""

  if (!workspace) return null

  const tenants = workspace.tenants ?? []
  const shown = tenants.slice(0, MAX_CHIPS)
  const hidden = tenants.length - shown.length
  const count = `${tenants.length} ${tenants.length === 1 ? "source" : "sources"}`
  const { Icon } = getProviderMeta(tenants[0]?.provider)

  return (
    <button
      type="button"
      data-testid="chat-workspace-scope"
      onClick={() => navigate(`${pathPrefix}${workspacePath(workspace)}`)}
      title={`Workspace: ${workspace.display_name}. Sources: ${
        tenants.map((t) => t.tenant_name).join(", ") || "none"
      }. Click to manage.`}
      className="flex w-full min-w-0 shrink-0 items-center gap-2 border-b px-4 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
    >
      <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="max-w-[40%] shrink-0 truncate font-medium text-foreground">
        {workspace.display_name}
      </span>
      <span className="shrink-0" data-testid="chat-workspace-scope-count">· {count}</span>
      <span className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
        {shown.map((tenant) => (
          <span
            key={tenant.id}
            data-testid={`chat-workspace-scope-source-${tenant.id}`}
            className={cn(
              "min-w-0 max-w-[12rem] truncate rounded px-1.5 py-0.5",
              PROVIDER_TINT[tenant.provider] ?? FALLBACK_TINT,
            )}
          >
            {tenant.tenant_name}
          </span>
        ))}
        {hidden > 0 && (
          <span className="shrink-0" data-testid="chat-workspace-scope-more">+{hidden}</span>
        )}
      </span>
      <ChevronRight className="h-3.5 w-3.5 shrink-0" aria-hidden />
    </button>
  )
}

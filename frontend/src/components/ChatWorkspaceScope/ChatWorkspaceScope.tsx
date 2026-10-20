import { useLocation, useNavigate } from "react-router-dom"
import { ChevronRight } from "lucide-react"
import { useAppStore } from "@/store/store"
import { useEmbedSettings } from "@/contexts/EmbedSettingsContext"
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
  const { mode } = useEmbedSettings()
  // Embeds without the sidebar have no way back to chat from the settings page.
  const canOpenSettings = pathPrefix === "" || mode === "full"

  if (!workspace) return null

  const tenants = workspace.tenants ?? []
  const shown = tenants.slice(0, MAX_CHIPS)
  const hidden = tenants.length - shown.length
  const count = `${tenants.length} ${tenants.length === 1 ? "source" : "sources"}`
  const providers = new Set(tenants.map((t) => t.provider))
  const { Icon } = getProviderMeta(providers.size === 1 ? tenants[0].provider : undefined)
  const summary = `Workspace ${workspace.display_name}, ${count}: ${
    tenants.map((t) => t.tenant_name).join(", ") || "none"
  }.`

  const content = (
    <>
      <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="min-w-[6rem] truncate font-medium text-foreground">
        {workspace.display_name}
      </span>
      <span className="shrink-0 whitespace-nowrap" data-testid="chat-workspace-scope-count">
        · {count}
      </span>
      {/* Below ~32rem the chips would shrink to blank pills; the count says enough. */}
      <span className="hidden min-w-0 flex-1 items-center gap-1 overflow-hidden @lg:flex">
        {shown.map((tenant) => (
          <span
            key={tenant.id}
            data-testid={`chat-workspace-scope-source-${tenant.id}`}
            className={cn(
              "min-w-[3rem] max-w-[12rem] truncate rounded px-1.5 py-0.5",
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
      {canOpenSettings && <ChevronRight className="ml-auto h-3.5 w-3.5 shrink-0" aria-hidden />}
    </>
  )
  const className =
    "@container flex w-full min-w-0 shrink-0 items-center gap-2 border-b px-4 py-1.5 text-left text-xs text-muted-foreground"

  if (!canOpenSettings) {
    return (
      <div
        data-testid="chat-workspace-scope"
        title={summary}
        aria-label={summary}
        role="group"
        className={className}
      >
        {content}
      </div>
    )
  }
  return (
    <button
      type="button"
      data-testid="chat-workspace-scope"
      onClick={() => navigate(`${pathPrefix}${workspacePath(workspace)}`)}
      title={summary}
      aria-label={`${summary} Open workspace settings.`}
      className={cn(
        className,
        "transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring",
      )}
    >
      {content}
    </button>
  )
}

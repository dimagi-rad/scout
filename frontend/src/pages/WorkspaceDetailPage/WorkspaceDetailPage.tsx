import { useState, useEffect, useMemo } from "react"
import { Link, useParams, useNavigate, useLocation } from "react-router-dom"
import { workspaceApi } from "@/api/workspaces"
import type { WorkspaceDetail } from "@/api/workspaces"
import { ApiError } from "@/api/client"
import { useAppStore } from "@/store/store"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { ChevronLeft } from "lucide-react"
import { RoleBadge } from "@/components/RoleBadge"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"
import { slugifyWorkspaceName, workspacePath } from "@/lib/workspacePath"
import { MembersTab } from "./MembersTab"
import { TenantsTab } from "./TenantsTab"
import { SettingsTab } from "./SettingsTab"

/** Friendly, human descriptor for a single provider on the settings header. */
const PROVIDER_DESCRIPTORS: Record<string, string> = {
  commcare_connect: "Connect opportunity",
  commcare: "CommCare project",
  ocs: "Open Chat Studio bot",
}

function providerDescriptor(provider: string): string {
  return PROVIDER_DESCRIPTORS[provider] ?? getProviderMeta(provider).label
}

/**
 * Muted icon + type descriptor shown under the workspace name. Sources the
 * provider list from the store `domains` (already loaded for the workspaces
 * list) so no extra backend field is needed. Renders nothing when the
 * workspace's providers aren't known client-side yet.
 */
function WorkspaceProviderType({ workspaceId }: { workspaceId: string }) {
  const domains = useAppStore((s) => s.domains)

  const providers = useMemo(() => {
    const ws = domains.find((d) => d.id === workspaceId)
    const tenants = ws?.tenants ?? []
    return [...new Set(tenants.map((t) => t.provider))]
  }, [domains, workspaceId])

  if (providers.length === 0) return null

  if (providers.length === 1) {
    const provider = providers[0]
    const { Icon } = getProviderMeta(provider)
    return (
      <div
        className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
        data-testid="workspace-provider-type"
      >
        <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span>{providerDescriptor(provider)}</span>
      </div>
    )
  }

  return (
    <div
      className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
      data-testid="workspace-provider-type"
    >
      {providers.map((provider) => {
        const { label, Icon } = getProviderMeta(provider)
        return <Icon key={provider} className="h-3.5 w-3.5 shrink-0" aria-hidden aria-label={label} />
      })}
      <span>Multiple sources</span>
    </div>
  )
}

export function WorkspaceDetailPage() {
  const { workspaceId, slug } = useParams<{ workspaceId: string; slug?: string }>()
  const [workspace, setWorkspace] = useState<WorkspaceDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  const location = useLocation()
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)

  // Keep the top-bar switcher in sync; on hard refresh `activeDomainId` would
  // otherwise default to domains[0] and show a different workspace.
  useEffect(() => {
    if (workspaceId) setActiveDomain(workspaceId)
  }, [workspaceId, setActiveDomain])

  function handleRename(newName: string) {
    setWorkspace((prev) => prev ? { ...prev, name: newName } : prev)
    fetchDomains()
  }

  function handleDelete() {
    fetchDomains()
    navigate(`${location.pathname.startsWith("/embed") ? "/embed" : ""}/workspaces`)
  }

  useEffect(() => {
    if (!workspaceId) return
    async function fetchWorkspace() {
      setLoading(true)
      // Clear prior error so an in-place reload doesn't keep rendering the stale
      // error screen — the render gate is `if (error || !workspace)`.
      setError(null)
      try {
        const data = await workspaceApi.getDetail(workspaceId!)
        setWorkspace(data)
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Failed to load workspace")
      } finally {
        setLoading(false)
      }
    }
    void fetchWorkspace()
  }, [workspaceId])

  // Canonicalize the URL to `/workspaces/<slug>/<uuid>` once loaded. Resolution
  // is by UUID, so this is cosmetic. Guarded on slug actually differing so it
  // can't loop; preserves the embed prefix like the switcher does.
  useEffect(() => {
    if (!workspace || workspace.id !== workspaceId) return
    const desiredSlug = slugifyWorkspaceName(workspace.display_name)
    if (!desiredSlug || slug === desiredSlug) return
    const pathPrefix = location.pathname.startsWith("/embed") ? "/embed" : ""
    navigate(`${pathPrefix}${workspacePath(workspace)}`, { replace: true })
  }, [workspace, workspaceId, slug, location.pathname, navigate])

  if (loading) return <div className="p-8 text-center text-muted-foreground">Loading…</div>
  if (error || !workspace) return <div className="p-8 text-center text-destructive">{error ?? "Workspace not found"}</div>

  const isManager = workspace.role === "manage"

  return (
    <div className="p-6">
      <div className="mb-6">
        <Link
          to="/workspaces"
          className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          data-testid="back-to-workspaces"
        >
          <ChevronLeft className="h-4 w-4" />
          Workspaces
        </Link>
        <div className="flex items-start gap-3">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-semibold" data-testid="workspace-name">
                {workspace.display_name}
              </h1>
              <RoleBadge role={workspace.role} />
            </div>
            <WorkspaceProviderType workspaceId={workspace.id} />
          </div>
        </div>
      </div>

      <Tabs defaultValue="members">
        <TabsList data-testid="workspace-tabs">
          <TabsTrigger value="members" data-testid="tab-members">Members</TabsTrigger>
          <TabsTrigger value="tenants" data-testid="tab-tenants">Data sources</TabsTrigger>
          <TabsTrigger value="settings" data-testid="tab-settings">Settings</TabsTrigger>
        </TabsList>

        <TabsContent value="members">
          <MembersTab workspaceId={workspace.id} isManager={isManager} />
        </TabsContent>

        <TabsContent value="tenants">
          <TenantsTab
            workspaceId={workspace.id}
            isManager={isManager}
            onWorkspaceDeleted={handleDelete}
          />
        </TabsContent>

        <TabsContent value="settings">
          <SettingsTab workspace={workspace} onRename={handleRename} onDelete={handleDelete} />
        </TabsContent>
      </Tabs>
    </div>
  )
}

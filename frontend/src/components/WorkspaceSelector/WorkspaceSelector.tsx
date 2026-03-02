import { useEffect, useMemo, useState } from "react"
import { Search, X, Plus, Users, Database, AlertTriangle } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"

type Tab = "custom" | "commcare" | "connect"

interface WorkspaceSelectorProps {
  open: boolean
  onClose: () => void
}

export function WorkspaceSelector({ open, onClose }: WorkspaceSelectorProps) {
  if (!open) return null
  return <WorkspaceSelectorPanel onClose={onClose} />
}

function WorkspaceSelectorPanel({ onClose }: { onClose: () => void }) {
  const [activeTab, setActiveTab] = useState<Tab>("custom")
  const [search, setSearch] = useState("")

  const domains = useAppStore((s) => s.domains)
  const customWorkspaces = useAppStore((s) => s.customWorkspaces)
  const enterError = useAppStore((s) => s.enterError)
  const missingTenants = useAppStore((s) => s.missingTenants)
  const fetchCustomWorkspaces = useAppStore((s) => s.workspaceActions.fetchCustomWorkspaces)
  const enterCustomWorkspace = useAppStore((s) => s.workspaceActions.enterCustomWorkspace)
  const exitCustomWorkspace = useAppStore((s) => s.workspaceActions.exitCustomWorkspace)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const newThread = useAppStore((s) => s.uiActions.newThread)

  const commcareDomains = useMemo(
    () => domains.filter((d) => d.provider === "commcare"),
    [domains],
  )
  const connectDomains = useMemo(
    () => domains.filter((d) => d.provider === "commcare_connect"),
    [domains],
  )

  // Fetch custom workspaces on mount
  useEffect(() => {
    fetchCustomWorkspaces()
  }, [fetchCustomWorkspaces])

  // Filter items based on search
  const filteredCustomWorkspaces = useMemo(() => {
    if (!search) return customWorkspaces
    const lower = search.toLowerCase()
    return customWorkspaces.filter((w) => w.name.toLowerCase().includes(lower))
  }, [customWorkspaces, search])

  const filteredCommcareDomains = useMemo(() => {
    if (!search) return commcareDomains
    const lower = search.toLowerCase()
    return commcareDomains.filter((d) => d.tenant_name.toLowerCase().includes(lower))
  }, [commcareDomains, search])

  const filteredConnectDomains = useMemo(() => {
    if (!search) return connectDomains
    const lower = search.toLowerCase()
    return connectDomains.filter((d) => d.tenant_name.toLowerCase().includes(lower))
  }, [connectDomains, search])

  const handleSelectDomain = (id: string) => {
    exitCustomWorkspace()
    setActiveDomain(id)
    newThread()
    onClose()
  }

  const handleEnterWorkspace = async (id: string) => {
    await enterCustomWorkspace(id)
    // Only close if there was no error (enterError will be set by the action if failed)
    const currentError = useAppStore.getState().enterError
    if (!currentError) {
      newThread()
      onClose()
    }
  }

  const tabs: { key: Tab; label: string; count: number }[] = [
    { key: "custom", label: "Custom", count: customWorkspaces.length },
    { key: "commcare", label: "CommCare", count: commcareDomains.length },
    { key: "connect", label: "Connect", count: connectDomains.length },
  ]

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      data-testid="workspace-selector-panel"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="relative w-full max-w-2xl rounded-lg border bg-background shadow-lg">
        {/* Header */}
        <div className="flex items-center justify-between border-b px-6 py-4">
          <h2 className="text-lg font-semibold">Select Workspace</h2>
          <button
            onClick={onClose}
            className="rounded-sm opacity-70 transition-opacity hover:opacity-100"
            data-testid="workspace-selector-close"
          >
            <X className="h-4 w-4" />
            <span className="sr-only">Close</span>
          </button>
        </div>

        {/* Error Banner */}
        {enterError && (
          <div
            className="mx-6 mt-4 flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
            data-testid="workspace-enter-error"
          >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <p className="font-medium">{enterError}</p>
              {missingTenants.length > 0 && (
                <ul className="mt-1 list-disc pl-4 text-xs">
                  {missingTenants.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}

        {/* Tabs */}
        <div className="flex gap-1 border-b px-6 pt-4">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => {
                setActiveTab(tab.key)
                setSearch("")
              }}
              data-testid={`workspace-tab-${tab.key}`}
              className={`rounded-t-md px-4 py-2 text-sm font-medium transition-colors ${
                activeTab === tab.key
                  ? "border-b-2 border-primary text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {tab.label} ({tab.count})
            </button>
          ))}
        </div>

        {/* Search */}
        <div className="px-6 pt-4">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder={`Search ${tabs.find((t) => t.key === activeTab)?.label ?? ""}...`}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-9"
              data-testid="workspace-search"
            />
          </div>
        </div>

        {/* Content */}
        <div className="max-h-80 overflow-y-auto px-6 py-4">
          {activeTab === "custom" && (
            <CustomTabContent
              workspaces={filteredCustomWorkspaces}
              onEnter={handleEnterWorkspace}
            />
          )}
          {activeTab === "commcare" && (
            <DomainTabContent
              domains={filteredCommcareDomains}
              onSelect={handleSelectDomain}
              emptyMessage="No CommCare domains found."
            />
          )}
          {activeTab === "connect" && (
            <DomainTabContent
              domains={filteredConnectDomains}
              onSelect={handleSelectDomain}
              emptyMessage="No Connect opportunities found."
            />
          )}
        </div>
      </div>
    </div>
  )
}

function CustomTabContent({
  workspaces,
  onEnter,
}: {
  workspaces: { id: string; name: string; tenant_count: number; member_count: number }[]
  onEnter: (id: string) => void
}) {
  return (
    <div className="space-y-2">
      {workspaces.length === 0 && (
        <p className="py-4 text-center text-sm text-muted-foreground">
          No custom workspaces found.
        </p>
      )}
      {workspaces.map((w) => (
        <button
          key={w.id}
          onClick={() => onEnter(w.id)}
          data-testid={`workspace-item-${w.id}`}
          className="flex w-full items-center justify-between rounded-md border px-4 py-3 text-left transition-colors hover:bg-accent"
        >
          <div>
            <p className="text-sm font-medium">{w.name}</p>
            <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
              <span className="flex items-center gap-1">
                <Database className="h-3 w-3" />
                {w.tenant_count} {w.tenant_count === 1 ? "tenant" : "tenants"}
              </span>
              <span className="flex items-center gap-1">
                <Users className="h-3 w-3" />
                {w.member_count} {w.member_count === 1 ? "member" : "members"}
              </span>
            </div>
          </div>
        </button>
      ))}
      <Button
        variant="outline"
        className="mt-2 w-full"
        data-testid="workspace-create-btn"
      >
        <Plus className="mr-2 h-4 w-4" />
        Create Custom Workspace
      </Button>
    </div>
  )
}

function DomainTabContent({
  domains,
  onSelect,
  emptyMessage,
}: {
  domains: { id: string; tenant_id: string; tenant_name: string }[]
  onSelect: (id: string) => void
  emptyMessage: string
}) {
  return (
    <div className="space-y-1">
      {domains.length === 0 && (
        <p className="py-4 text-center text-sm text-muted-foreground">{emptyMessage}</p>
      )}
      {domains.map((d) => (
        <button
          key={d.id}
          onClick={() => onSelect(d.id)}
          data-testid={`workspace-domain-${d.tenant_id}`}
          className="flex w-full items-center rounded-md px-4 py-2.5 text-left text-sm transition-colors hover:bg-accent"
        >
          {d.tenant_name}
        </button>
      ))}
    </div>
  )
}

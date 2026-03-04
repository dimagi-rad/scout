import { useMemo, useState } from "react"
import { Search } from "lucide-react"
import { Input } from "@/components/ui/input"
import { tenantDisplayName, type TenantMembership } from "@/store/domainSlice"

interface DomainPickerProps {
  domains: TenantMembership[]
  mode: "single" | "multi"
  // single-select mode
  onSelect?: (id: string) => void
  // multi-select mode
  selectedIds?: Set<string>
  onToggle?: (tenantId: string) => void
  // optional slot for a "Custom" tab (WorkspaceSelector passes this, CreateWorkspaceForm does not)
  customTab?: { label: string; count: number; content: React.ReactNode }
}

type BuiltInTab = "commcare" | "connect"
type Tab = "custom" | BuiltInTab

export function DomainPicker({
  domains,
  mode,
  onSelect,
  selectedIds,
  onToggle,
  customTab,
}: DomainPickerProps) {
  const [activeTab, setActiveTab] = useState<Tab>(customTab ? "custom" : "commcare")
  const [search, setSearch] = useState("")

  const commcareDomains = useMemo(
    () => domains.filter((d) => d.provider === "commcare"),
    [domains],
  )
  const connectDomains = useMemo(
    () => domains.filter((d) => d.provider === "commcare_connect"),
    [domains],
  )

  const filteredCommcareDomains = useMemo(() => {
    if (!search) return commcareDomains
    const lower = search.toLowerCase()
    return commcareDomains.filter((d) => tenantDisplayName(d).toLowerCase().includes(lower))
  }, [commcareDomains, search])

  const filteredConnectDomains = useMemo(() => {
    if (!search) return connectDomains
    const lower = search.toLowerCase()
    return connectDomains.filter((d) => tenantDisplayName(d).toLowerCase().includes(lower))
  }, [connectDomains, search])

  const tabs: { key: Tab; label: string; count: number }[] = [
    ...(customTab
      ? [{ key: "custom" as Tab, label: customTab.label, count: customTab.count }]
      : []),
    { key: "commcare", label: "CommCare", count: commcareDomains.length },
    { key: "connect", label: "Connect", count: connectDomains.length },
  ]

  const activeLabel = tabs.find((t) => t.key === activeTab)?.label ?? ""

  return (
    <>
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
            placeholder={`Search ${activeLabel}...`}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
            data-testid="workspace-search"
          />
        </div>
      </div>

      {/* Content */}
      <div className="max-h-80 overflow-y-auto px-6 py-4">
        {activeTab === "custom" && customTab?.content}
        {activeTab === "commcare" && (
          <DomainList
            domains={filteredCommcareDomains}
            mode={mode}
            onSelect={onSelect}
            selectedIds={selectedIds}
            onToggle={onToggle}
            emptyMessage="No CommCare domains found."
          />
        )}
        {activeTab === "connect" && (
          <DomainList
            domains={filteredConnectDomains}
            mode={mode}
            onSelect={onSelect}
            selectedIds={selectedIds}
            onToggle={onToggle}
            emptyMessage="No Connect opportunities found."
          />
        )}
      </div>
    </>
  )
}

function DomainList({
  domains,
  mode,
  onSelect,
  selectedIds,
  onToggle,
  emptyMessage,
}: {
  domains: TenantMembership[]
  mode: "single" | "multi"
  onSelect?: (id: string) => void
  selectedIds?: Set<string>
  onToggle?: (tenantId: string) => void
  emptyMessage: string
}) {
  if (domains.length === 0) {
    return (
      <p className="py-4 text-center text-sm text-muted-foreground">{emptyMessage}</p>
    )
  }

  if (mode === "single") {
    return (
      <div className="space-y-1">
        {domains.map((d) => (
          <button
            key={d.id}
            onClick={() => onSelect?.(d.id)}
            data-testid={`workspace-domain-${d.tenant_id}`}
            className="flex w-full items-center rounded-md px-4 py-2.5 text-left text-sm transition-colors hover:bg-accent"
          >
            {tenantDisplayName(d)}
          </button>
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-1">
      {domains.map((d) => (
        <label
          key={d.id}
          className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-accent"
          data-testid={`create-workspace-tenant-${d.tenant_id}`}
        >
          <input
            type="checkbox"
            checked={selectedIds?.has(d.tenant_id) ?? false}
            onChange={() => onToggle?.(d.tenant_id)}
            className="h-4 w-4 rounded border-input"
          />
          {tenantDisplayName(d)}
        </label>
      ))}
    </div>
  )
}

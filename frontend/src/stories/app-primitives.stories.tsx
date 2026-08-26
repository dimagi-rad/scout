import { useEffect, useState } from "react"
import type { ReactNode } from "react"
import { BarChart3, Database, MessageSquare, Settings } from "lucide-react"
import { MemoryRouter } from "react-router-dom"
import type { Meta, StoryObj } from "@storybook/react-vite"

import { ChatEmptyState } from "@/components/ChatEmptyState"
import { MaterializationFailure } from "@/components/MaterializationStatus/MaterializationFailure"
import { MaterializationProgressBanner } from "@/components/MaterializationStatus/MaterializationProgressBanner"
import { NavItem } from "@/components/Sidebar/NavItem"
import { RoleBadge } from "@/components/RoleBadge"
import { SearchFilterBar } from "@/components/SearchFilterBar/SearchFilterBar"
import { SlashCommandMenu } from "@/components/ChatPanel/SlashCommandMenu"
import { WorkspaceSwitcher } from "@/components/WorkspaceSwitcher"
import { useAppStore } from "@/store/store"
import type { ActiveJob, RecentTermination } from "@/api/jobs"
import type { TenantMembership } from "@/store/domainSlice"

const workspaces: TenantMembership[] = [
  {
    id: "workspace-1",
    name: "global-operations",
    display_name: "Global Operations",
    is_auto_created: false,
    role: "manage",
    tenants: [{ id: "tenant-1", tenant_name: "CommCare HQ", provider: "commcare" }],
    member_count: 8,
    schema_status: "available",
    last_synced_at: new Date(Date.now() - 42 * 60 * 1000).toISOString(),
    created_at: "2026-01-10T12:00:00Z",
  },
  {
    id: "workspace-2",
    name: "field-payments",
    display_name: "Field Payments",
    is_auto_created: false,
    role: "read_write",
    tenants: [
      { id: "tenant-2", tenant_name: "Connect Payments", provider: "commcare_connect" },
    ],
    member_count: 4,
    schema_status: "provisioning",
    last_synced_at: null,
    created_at: "2026-02-12T12:00:00Z",
  },
  {
    id: "workspace-3",
    name: "support-bots",
    display_name: "Support Bots",
    is_auto_created: false,
    role: "read",
    tenants: [{ id: "tenant-3", tenant_name: "Open Chat Studio", provider: "ocs" }],
    member_count: 3,
    schema_status: "unavailable",
    last_synced_at: null,
    created_at: "2026-03-14T12:00:00Z",
  },
]

const activeJob: ActiveJob = {
  thread_job_id: "job-1",
  thread_id: "thread-1",
  tool_call_id: "tool-1",
  job_type: "materialization",
  state: "running",
  created_at: "2026-06-26T12:00:00Z",
  progress: {
    percent: 64,
    rows_loaded: 32000,
    rows_total: 50000,
    unit: "rows",
    message: null,
    source: "CommCare",
    step: 2,
    total_steps: 3,
  },
}

const indeterminateJob: ActiveJob = {
  ...activeJob,
  thread_job_id: "job-2",
  progress: {
    percent: null,
    rows_loaded: 1200,
    rows_total: null,
    unit: "sessions",
    message: null,
    source: "Open Chat Studio",
    step: null,
    total_steps: null,
  },
}

const failure: RecentTermination = {
  thread_job_id: "job-3",
  thread_id: "thread-1",
  tool_call_id: "tool-1",
  state: "failed",
  completed_at: "2026-06-26T12:30:00Z",
  error_summary: "The source returned a 403 while fetching the forms table.",
  retry_available: true,
}

const meta = {
  title: "App Primitives/Controls and Status",
  tags: ["autodocs"],
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

function MockWorkspaceStore({ children }: { children: ReactNode }) {
  useEffect(() => {
    useAppStore.setState({
      domains: workspaces,
      activeDomainId: "workspace-1",
      domainsStatus: "loaded",
      domainsError: null,
    })
    window.localStorage.setItem(
      "scout.recentWorkspaces",
      JSON.stringify(["workspace-1", "workspace-2"]),
    )
  }, [])

  return children
}

export const RoleBadges: Story = {
  render: () => (
    <div className="flex flex-wrap items-center gap-2">
      <RoleBadge role="manage" />
      <RoleBadge role="read_write" />
      <RoleBadge role="read" />
      <RoleBadge role="custom_role" />
    </div>
  ),
}

export const SearchAndFilters: Story = {
  render: function SearchAndFiltersStory() {
    const [search, setSearch] = useState("cases")
    const [activeFilters, setActiveFilters] = useState<Record<string, string | null>>({
      source: "commcare",
      status: null,
    })

    return (
      <div className="w-[760px]">
        <SearchFilterBar
          search={search}
          onSearchChange={setSearch}
          placeholder="Search tables..."
          filters={[
            {
              name: "source",
              options: [
                { value: "commcare", label: "CommCare", count: 12 },
                { value: "connect", label: "Connect", count: 5 },
                { value: "ocs", label: "OCS", count: 3 },
              ],
            },
            {
              name: "status",
              options: [
                { value: "ready", label: "Ready", count: 14 },
                { value: "syncing", label: "Syncing", count: 2 },
              ],
            },
          ]}
          activeFilters={activeFilters}
          onFilterChange={(group, value) =>
            setActiveFilters((current) => ({ ...current, [group]: value }))
          }
        />
      </div>
    )
  },
}

export const NavigationItems: Story = {
  render: () => (
    <MemoryRouter initialEntries={["/workspaces/workspace-1/chat"]}>
      <nav className="grid w-[260px] gap-1 rounded-lg border p-2">
        <NavItem
          to="/workspaces/workspace-1/chat"
          icon={MessageSquare}
          label="Chat"
          isActivePath={(pathname) => pathname.includes("/chat")}
        />
        <NavItem to="/datasets" icon={Database} label="Datasets" />
        <NavItem to="/artifacts" icon={BarChart3} label="Artifacts" />
        <NavItem to="/settings" icon={Settings} label="Settings" />
      </nav>
    </MemoryRouter>
  ),
}

export const WorkspaceSwitchers: Story = {
  render: () => (
    <MemoryRouter initialEntries={["/workspaces/workspace-1/chat"]}>
      <MockWorkspaceStore>
        <div className="grid w-[420px] gap-8">
          <div className="space-y-2">
            <div className="text-sm font-medium">Sidebar variant</div>
            <WorkspaceSwitcher />
          </div>
          <div className="space-y-2">
            <div className="text-sm font-medium">Topbar variant</div>
            <div className="flex justify-end rounded-lg border p-3">
              <WorkspaceSwitcher variant="topbar" />
            </div>
          </div>
        </div>
      </MockWorkspaceStore>
    </MemoryRouter>
  ),
}

export const SlashCommands: Story = {
  render: () => (
    <div className="relative w-[520px] rounded-lg border p-4">
      <div className="rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground">
        /ref
      </div>
      <SlashCommandMenu
        query="r"
        visible
        selectedIndex={0}
        onSelect={() => undefined}
      />
    </div>
  ),
}

export const MaterializationStatus: Story = {
  parameters: {
    layout: "fullscreen",
  },
  render: () => (
    <div className="mx-auto grid max-w-3xl gap-4 py-10">
      <MaterializationProgressBanner job={activeJob} workspaceId="workspace-1" />
      <MaterializationProgressBanner job={indeterminateJob} workspaceId="workspace-1" />
      <div className="px-4">
        <MaterializationFailure
          termination={failure}
          workspaceId="workspace-1"
          threadId="thread-1"
        />
      </div>
    </div>
  ),
}

export const EmptyChat: Story = {
  parameters: {
    layout: "fullscreen",
  },
  render: function EmptyChatStory() {
    const [input, setInput] = useState("")
    return (
      <MockWorkspaceStore>
        <div className="min-h-[620px]">
          <ChatEmptyState input={input} setInput={setInput} onSend={() => undefined} />
        </div>
      </MockWorkspaceStore>
    )
  },
}

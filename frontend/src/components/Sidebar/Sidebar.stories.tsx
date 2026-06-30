import { useEffect, type ReactNode } from "react"
import { MemoryRouter } from "react-router-dom"
import type { Meta, StoryObj } from "@storybook/react-vite"

import { WorkspaceJobsProvider } from "@/contexts/WorkspaceJobsContext"
import { useAppStore } from "@/store/store"
import type { TenantMembership } from "@/store/domainSlice"
import type { Thread, ThreadsStatus } from "@/store/uiSlice"
import { Sidebar } from "./Sidebar"

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
    last_synced_at: "2026-06-30T12:00:00Z",
    created_at: "2026-01-10T12:00:00Z",
  },
  {
    id: "workspace-2",
    name: "payments-pilot",
    display_name: "Payments Pilot",
    is_auto_created: false,
    role: "read_write",
    tenants: [
      { id: "tenant-2", tenant_name: "Connect Payments", provider: "commcare_connect" },
    ],
    member_count: 5,
    schema_status: "provisioning",
    last_synced_at: null,
    created_at: "2026-02-12T12:00:00Z",
  },
]

const threads: Thread[] = [
  {
    id: "thread-1",
    title: "Verified visits by worker",
    created_at: "2026-06-29T13:10:00Z",
    updated_at: "2026-06-30T12:35:00Z",
    last_viewed_at: "2026-06-30T11:00:00Z",
    is_shared: false,
    is_public: false,
    share_token: null,
  },
  {
    id: "thread-2",
    title: "Payment reconciliation for June",
    created_at: "2026-06-27T09:00:00Z",
    updated_at: "2026-06-27T10:20:00Z",
    last_viewed_at: "2026-06-27T10:20:00Z",
    is_shared: true,
    is_public: false,
    share_token: "story-token",
  },
  {
    id: "thread-3",
    title: "Long-running materialization review",
    created_at: "2026-06-20T14:00:00Z",
    updated_at: "2026-06-21T15:40:00Z",
    last_viewed_at: "2026-06-21T15:40:00Z",
    is_shared: false,
    is_public: false,
    share_token: null,
  },
]

interface SidebarStoryProps {
  initialPath?: string
  threadsStatus?: ThreadsStatus
  children?: ReactNode
}

function SeedSidebarStore({ children, threadsStatus = "loaded" }: SidebarStoryProps) {
  useEffect(() => {
    const state = useAppStore.getState()
    useAppStore.setState({
      user: {
        id: "story-user",
        email: "operator@example.org",
        name: "Story Operator",
        is_staff: false,
        onboarding_complete: true,
      },
      authStatus: "authenticated",
      authError: null,
      domains: workspaces,
      activeDomainId: "workspace-1",
      domainsStatus: "loaded",
      domainsError: null,
      threadId: "thread-1",
      threads,
      threadsStatus,
      activeArtifactId: null,
      domainActions: {
        ...state.domainActions,
        fetchDomains: async () => undefined,
        setActiveDomain: (id: string) => {
          useAppStore.setState({ activeDomainId: id, threadId: "story-thread-new" })
        },
      },
      uiActions: {
        ...state.uiActions,
        fetchThreads: async () => undefined,
        newThread: () => {
          useAppStore.setState({ threadId: "story-thread-new", activeArtifactId: null })
        },
        selectThread: async (id: string) => {
          useAppStore.setState({ threadId: id, activeArtifactId: null })
        },
      },
      authActions: {
        ...state.authActions,
        logout: async () => {
          useAppStore.setState({ user: null, authStatus: "unauthenticated" })
        },
      },
    })
    window.localStorage.setItem(
      "scout.recentWorkspaces",
      JSON.stringify(["workspace-1", "workspace-2"]),
    )
  }, [threadsStatus])

  return children
}

function SidebarStoryFrame({
  initialPath = "/workspaces/global-operations/workspace-1/chat/thread-1",
  threadsStatus,
  children,
}: SidebarStoryProps) {
  return (
    <MemoryRouter initialEntries={[initialPath]}>
      <SeedSidebarStore threadsStatus={threadsStatus}>
        <WorkspaceJobsProvider workspaceId={null}>
          <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
            <Sidebar />
            <main className="flex min-w-0 flex-1 flex-col border-l bg-muted/20">
              {children ?? (
                <div className="flex h-full flex-col px-6 py-5">
                  <div className="text-sm font-medium">Verified visits by worker</div>
                  <div className="mt-2 max-w-md text-sm text-muted-foreground">
                    Global Operations - updated 12 minutes ago
                  </div>
                </div>
              )}
            </main>
          </div>
        </WorkspaceJobsProvider>
      </SeedSidebarStore>
    </MemoryRouter>
  )
}

const meta = {
  title: "App Shell/Sidebar",
  component: Sidebar,
  tags: ["autodocs"],
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta<typeof Sidebar>

export default meta
type Story = StoryObj<typeof meta>

export const Desktop: Story = {
  render: () => <SidebarStoryFrame />,
}

export const NarrowRail: Story = {
  parameters: {
    viewport: {
      defaultViewport: "mobile2",
    },
  },
  render: () => <SidebarStoryFrame />,
}

export const ThreadLoadError: Story = {
  render: () => <SidebarStoryFrame threadsStatus="error" />,
}

export const EmbedMode: Story = {
  render: () => (
    <SidebarStoryFrame initialPath="/embed/workspaces/global-operations/workspace-1/chat/thread-1" />
  ),
}

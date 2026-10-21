import { useEffect } from "react"
import { MemoryRouter } from "react-router-dom"
import type { Meta, StoryObj } from "@storybook/react-vite"
import type { WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { WorkspaceSwitcher } from "./WorkspaceSwitcher"

const baseWorkspaces: WorkspaceListItem[] = [
  { id: "recorded", display_name: "Example — recorded import", schema_status: "available" },
  { id: "loading", display_name: "Example — loading data", schema_status: "provisioning" },
  { id: "failed", display_name: "Example — setup failed", schema_status: "failed" },
  { id: "not-loaded", display_name: "Example — no data imported", schema_status: "not_loaded" },
  { id: "undated", display_name: "Example — no load time recorded", schema_status: "available" },
].map((state, index) => ({
  ...state,
  schema_status: state.schema_status as WorkspaceListItem["schema_status"],
  name: state.id,
  is_auto_created: false,
  role: "manage",
  tenants: [{ id: `tenant-${index}`, tenant_name: "Illustrative source", provider: index % 2 ? "ocs" : "commcare" }],
  member_count: 1,
  last_synced_at: state.id === "undated" || state.id === "not-loaded" ? null : "2026-04-16T12:00:00Z",
  created_at: "2026-01-01T12:00:00Z",
}))

const programWorkspace: WorkspaceListItem = {
  ...baseWorkspaces[0],
  id: "program",
  name: "program",
  display_name: "Example — program across opportunities",
  tenants: ["Kenya", "Uganda", "Malawi", "Nigeria"].map((country) => ({
    id: `tenant-${country}`,
    tenant_name: `Example program ${country}`,
    provider: "commcare_connect",
  })),
}
const workspaces = [programWorkspace, ...baseWorkspaces]

function LoadHistoryFixture({ variant }: { variant: "sidebar" | "topbar" }) {
  useEffect(() => {
    const previous = useAppStore.getState()
    useAppStore.setState({ domains: workspaces, activeDomainId: "program" })
    return () => useAppStore.setState(previous)
  }, [])

  return (
    <MemoryRouter>
      <div className="min-h-screen bg-background p-4 text-foreground">
        <div className={variant === "topbar" ? "flex justify-end" : "w-64 max-w-full"}>
          <WorkspaceSwitcher variant={variant} />
        </div>
      </div>
    </MemoryRouter>
  )
}

const meta = {
  title: "Workspaces/WorkspaceSwitcher",
  component: WorkspaceSwitcher,
  parameters: { layout: "fullscreen" },
  render: (args) => <LoadHistoryFixture variant={args?.variant ?? "sidebar"} />,
} satisfies Meta<typeof WorkspaceSwitcher>

export default meta
type Story = StoryObj<typeof meta>

export const RecordedLoadStates: Story = { args: { variant: "sidebar" } }
export const TopbarLoadStates: Story = { args: { variant: "topbar" } }

import type { Meta, StoryObj } from "@storybook/react-vite"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { CONNECTIONS_PATH } from "@/lib/routes"
import { LostAccessModal } from "./LostAccessModal"

const meta = {
  title: "Recovery/Lost Workspace Access",
  component: LostAccessModal,
  parameters: { layout: "fullscreen" },
  beforeEach: () => {
    const previous = useAppStore.getState()
    useAppStore.setState({
      domainsStatus: "loaded",
      activeDomainId: "recovery-demo",
      domains: [{
        id: "recovery-demo", name: "Example fieldwork", display_name: "Example fieldwork",
        is_auto_created: false, role: "manage", has_access: false, member_count: 1,
        schema_status: "available", last_synced_at: null, created_at: "2026-01-01T00:00:00Z",
        tenants: [{ id: "example-data", tenant_name: "Example data", provider: "commcare" }],
      }],
    })
    return () => useAppStore.setState({
      domains: previous.domains, domainsStatus: previous.domainsStatus,
      activeDomainId: previous.activeDomainId,
    })
  },
  decorators: [(Story) => (
    <MemoryRouter initialEntries={["/workspaces/recovery-demo/chat"]}>
      <Story />
      <Routes>
        <Route path={CONNECTIONS_PATH} element={
          <main className="p-6">
            <h1 className="text-2xl font-semibold">Connected Accounts</h1>
            <p className="mt-3 text-sm text-muted-foreground">
              Synthetic recovery destination. The lost-access gate no longer covers this page.
            </p>
          </main>
        } />
        <Route path="*" element={null} />
      </Routes>
    </MemoryRouter>
  )],
} satisfies Meta<typeof LostAccessModal>

export default meta
type Story = StoryObj<typeof meta>

export const NoAccessibleWorkspaces: Story = {}

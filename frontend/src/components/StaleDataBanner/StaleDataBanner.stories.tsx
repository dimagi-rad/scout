import { MemoryRouter } from "react-router-dom"
import type { Meta, StoryObj } from "@storybook/react-vite"
import {
  workspaceApi,
  type WorkspaceDetail,
  type WorkspaceListItem,
  type WorkspaceSourceFreshness,
} from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { StaleDataBanner } from "./StaleDataBanner"

const HOUR = 3600_000

function source(
  name: string,
  hoursAgo: number,
  extra: Partial<WorkspaceSourceFreshness> = {},
): WorkspaceSourceFreshness {
  return {
    tenant_id: name,
    tenant_name: name,
    provider: "commcare",
    provider_label: "CommCare HQ",
    last_synced_at: new Date(Date.now() - hoursAgo * HOUR).toISOString(),
    serving: true,
    ...extra,
  }
}

interface FixtureArgs {
  role: WorkspaceListItem["role"]
  sources: WorkspaceSourceFreshness[]
}

// beforeEach serves the args as the detail payload; the key remounts per story.
function Fixture(args: FixtureArgs) {
  return (
    <MemoryRouter>
      <div className="min-h-screen bg-background py-4 text-foreground">
        <StaleDataBanner key={JSON.stringify(args)} workspaceId="story-ws" />
      </div>
    </MemoryRouter>
  )
}

const meta = {
  title: "Chat/StaleDataBanner",
  component: Fixture,
  parameters: { layout: "fullscreen" },
  beforeEach: ({ args }) => {
    const previous = useAppStore.getState()
    const getDetail = workspaceApi.getDetail
    useAppStore.setState({ domains: [{ id: "story-ws", role: args.role } as WorkspaceListItem] })
    workspaceApi.getDetail = async () =>
      ({ id: "story-ws", sources: args.sources, stale_data_banner_hours: 24 }) as WorkspaceDetail
    try {
      sessionStorage.removeItem("scout:stale-banner-dismissed:story-ws")
    } catch {
      // Storybook may run without storage; the story still renders.
    }
    return () => {
      workspaceApi.getDetail = getDetail
      useAppStore.setState(previous)
    }
  },
} satisfies Meta<typeof Fixture>

export default meta
type Story = StoryObj<typeof meta>

export const Writer: Story = { args: { role: "read_write", sources: [source("Alpha", 72)] } }

export const HoursOld: Story = { args: { role: "manage", sources: [source("Alpha", 30)] } }

export const ReadOnly: Story = { args: { role: "read", sources: [source("Alpha", 72)] } }

export const OldestOfSeveral: Story = {
  args: { role: "manage", sources: [source("Alpha", 2), source("Beta", 100)] },
}

export const SignInExpired: Story = {
  args: {
    role: "manage",
    sources: [source("Alpha", 72, { reconnect: true, not_refreshed: true, last_load: "skipped" })],
  },
}

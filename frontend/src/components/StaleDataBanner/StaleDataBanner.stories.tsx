import { MemoryRouter } from "react-router-dom"
import type { Meta, StoryObj } from "@storybook/react-vite"
import {
  workspaceApi,
  type SourceFreshnessDetail,
  type WorkspaceListItem,
} from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { StaleDataBanner } from "./StaleDataBanner"
import { freshness, freshSource as source } from "./testFixtures"

interface FixtureArgs {
  role: WorkspaceListItem["role"]
  sources: SourceFreshnessDetail[]
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
    const getFreshness = workspaceApi.getFreshness
    useAppStore.setState({ domains: [{ id: "story-ws", role: args.role } as WorkspaceListItem] })
    workspaceApi.getFreshness = async () => freshness(args.sources)
    try {
      sessionStorage.removeItem("scout:stale-banner-dismissed:story-ws")
    } catch {
      // Storybook may run without storage; the story still renders.
    }
    return () => {
      workspaceApi.getFreshness = getFreshness
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

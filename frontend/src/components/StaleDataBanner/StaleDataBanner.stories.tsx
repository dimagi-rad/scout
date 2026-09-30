import { MemoryRouter } from "react-router-dom"
import type { Meta, StoryObj } from "@storybook/react-vite"
import type { SourceFreshnessDetail, WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { StaleDataBanner } from "./StaleDataBanner"
import { clearStaleBannerDismissal } from "./staleData"
import { freshness, freshSource as source } from "./testFixtures"

interface FixtureArgs {
  role: WorkspaceListItem["role"]
  sources: SourceFreshnessDetail[]
}

// The key remounts per story, so a dismiss in one does not carry to the next.
function Fixture(args: FixtureArgs) {
  return (
    <MemoryRouter>
      <div className="min-h-screen bg-background py-4 text-foreground">
        <StaleDataBanner
          key={JSON.stringify(args)}
          workspaceId="story-ws"
          freshness={freshness(args.sources)}
        />
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
    useAppStore.setState({ domains: [{ id: "story-ws", role: args.role } as WorkspaceListItem] })
    clearStaleBannerDismissal("story-ws")
    return () => {
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
    sources: [source("Alpha", 72, { reconnect: true })],
  },
}

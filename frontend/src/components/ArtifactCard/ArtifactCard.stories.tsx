import type { Meta, StoryObj } from "@storybook/react-vite"

import { ArtifactCard } from "@/components/ArtifactCard"
import type { ArtifactSummary } from "@/store/artifactSlice"

const storyArtifact: ArtifactSummary = {
  id: "artifact-story-1",
  title: "Verified Visits - Test Artifact",
  description: "Test story artifact backed by live semantic queries against the workspace model.",
  artifact_type: "story",
  version: 1,
  has_live_queries: true,
  created_at: "2026-06-30T14:20:00Z",
  updated_at: "2026-06-30T14:20:00Z",
}

const meta = {
  title: "App Primitives/ArtifactCard",
  component: ArtifactCard,
  tags: ["autodocs"],
  parameters: {
    layout: "centered",
  },
  args: {
    artifact: storyArtifact,
    onOpen: () => undefined,
    onUpdate: async () => undefined,
    onDelete: () => undefined,
  },
} satisfies Meta<typeof ArtifactCard>

export default meta
type Story = StoryObj<typeof meta>

export const StoryArtifact: Story = {
  render: (args) => (
    <div className="w-[22rem] max-w-[calc(100vw-2rem)]">
      <ArtifactCard {...args} />
    </div>
  ),
}

export const LongContentResponsive: Story = {
  args: {
    artifact: {
      ...storyArtifact,
      id: "artifact-story-long",
      title: "Verified Visits by Community Health Worker, Program Site, and Payment Window",
      description:
        "A longer story artifact description that should clamp cleanly without pushing actions outside the card or changing the grid width.",
      version: 3,
    },
  },
  render: (args) => (
    <div className="grid w-[18rem] max-w-[calc(100vw-2rem)] gap-4">
      <ArtifactCard {...args} />
    </div>
  ),
}

export const ResponsiveGrid: Story = {
  render: (args) => {
    const artifacts: ArtifactSummary[] = [
      args.artifact,
      {
        ...storyArtifact,
        id: "artifact-react-1",
        title: "Worker Completion Funnel",
        description: "Generated React artifact for reviewing visit completion by week.",
        artifact_type: "react",
        has_live_queries: false,
      },
      {
        ...storyArtifact,
        id: "artifact-markdown-1",
        title: "Weekly Program Notes",
        description: "Markdown summary created during the last workspace analysis.",
        artifact_type: "markdown",
        version: 2,
      },
    ]

    return (
      <div className="grid w-[58rem] max-w-[calc(100vw-2rem)] grid-cols-[repeat(auto-fill,minmax(min(100%,18rem),1fr))] gap-4">
        {artifacts.map((artifact) => (
          <ArtifactCard
            key={artifact.id}
            {...args}
            artifact={artifact}
          />
        ))}
      </div>
    )
  },
}

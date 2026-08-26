import type { Meta, StoryObj } from "@storybook/react-vite"

import { ArtifactGraphRenderer } from "@/components/ArtifactGraph"
import type { ArtifactDetail } from "@/components/ArtifactGraph"
import { storyArtifact } from "@/pages/ArtifactDemoPage/demoData"

import type { StoryDoc } from "./types"

const storyDoc = storyArtifact.data.story_doc as StoryDoc
const metricsAndComparisonArtifact: ArtifactDetail = {
  ...storyArtifact,
  id: "artifact-demo-metrics-and-comparison",
  title: "Metric and comparison block reference",
  data: {
    story_doc: {
      ...storyDoc,
      prd: "A focused, API-independent reference for Scout's production date, comparison, and metric blocks. All values are synthetic literal data.",
      blocks: storyDoc.blocks.filter((block) =>
        [
          "title",
          "controls-note",
          "date-filter",
          "period-selector",
          "approved-stat",
          "pending-stat",
          "completion-stat",
          "payment-stat",
        ].includes(block.id),
      ),
    },
  },
}

const meta = {
  title: "Artifact System/Production Renderer",
  component: ArtifactGraphRenderer,
  tags: ["autodocs"],
  parameters: {
    layout: "fullscreen",
    docs: {
      description: {
        component:
          "Durable design references for Scout's production Story artifact renderer. These stories reuse the synthetic literal fixture from the in-app artifact showcase, so they render without an API, authenticated session, or semantic model.",
      },
    },
  },
  args: {
    artifact: storyArtifact,
    workspaceId: "storybook-artifact-workspace",
  },
  decorators: [
    (Story) => (
      <div className="h-screen min-h-[44rem] bg-background text-foreground">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ArtifactGraphRenderer>

export default meta
type Story = StoryObj<typeof meta>

export const CompleteArtifact: Story = {
  parameters: {
    docs: {
      description: {
        story:
          "The complete production-rendered Story artifact: title, narrative blocks, date controls, comparison controls, metric cards, Recharts visualizations, a detail table, and provenance copy.",
      },
    },
  },
}

export const MetricsAndComparison: Story = {
  args: {
    artifact: metricsAndComparisonArtifact,
  },
  parameters: {
    docs: {
      description: {
        story:
          "A focused visual-regression reference for the date range, comparison period, and KPI block language. Use this story when changing metric hierarchy, delta treatment, period labels, or responsive grouping.",
      },
    },
  },
}

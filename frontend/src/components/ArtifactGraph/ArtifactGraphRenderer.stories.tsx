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

export const InvalidDateControls: Story = {
  args: {
    artifact: {
      ...storyArtifact,
      id: "invalid-date-controls",
      title: "Date control recovery",
      semantic_queries: [],
      data: { story_doc: { blocks: [
        { id: "title", type: "title", config: { text: "Date control recovery" } },
        { id: "range", type: "date_filter", config: { default: "last_60_days" } },
        { id: "period", type: "period_selector", config: { default_range: "this_week" } },
        { id: "context", type: "section", config: {
          title: "The rest of the artifact stays readable",
          body: "These intentionally invalid presets demonstrate a saved artifact that needs repair. No live data is queried, and no substitute date range is used.",
        } },
      ] } },
    },
  },
}

export const LongCategoryLabels: Story = {
  args: {
    artifact: {
      ...storyArtifact,
      id: "long-category-labels",
      title: "Long category labels",
      semantic_queries: [],
      data: { story_doc: { prd: "Internal brief that must not be shown.", blocks: [
        { id: "title", type: "title", config: { text: "Long category labels" } },
        {
          id: "chart",
          type: "graph",
          inputs: { data: { value: [
            { facility: "Clinic A", visits: 42 },
            { facility: "Northern District Community Health Outreach Programme", visits: 31 },
            { facility: "St. Mary's Referral Hospital Maternity Wing", visits: 18 },
          ] } },
          config: {
            title: "Visits by facility",
            chart_type: "bar",
            x_key: "facility",
            series: [{ data_key: "visits", label: "Visits" }],
            style: { orientation: "horizontal", legend: "none" },
            height: 260,
          },
        },
      ] } },
    },
  },
  parameters: {
    docs: {
      description: {
        story:
          "Horizontal bar chart whose category axis is sized from the longest label, capped, with an ellipsis and a hover tooltip carrying the full text.",
      },
    },
  },
}

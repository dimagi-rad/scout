import type { Meta, StoryObj } from "@storybook/react-vite"

import { ArtifactGraphRenderer } from "@/components/ArtifactGraph"
import type { ArtifactDetail } from "@/components/ArtifactGraph"
import { storyArtifact } from "@/pages/ArtifactDemoPage/demoData"

import type { StoryBlock, StoryDoc } from "./types"

const sourceDoc = storyArtifact.data.story_doc as StoryDoc

function sourceBlock(id: string): StoryBlock {
  const block = sourceDoc.blocks.find((candidate) => candidate.id === id)
  if (!block) throw new Error(`Missing Storybook block fixture: ${id}`)
  return block
}

function blockArtifact(slug: string, title: string, blocks: StoryBlock[]): ArtifactDetail {
  return {
    ...storyArtifact,
    id: `artifact-block-${slug}`,
    title,
    data: {
      story_doc: {
        schema_version: 1,
        name: title,
        blocks,
      },
    },
    semantic_queries: [],
    semantic_query_manifest: { entries: [], unresolved: [] },
    version: 1,
  }
}

const listSummary: StoryBlock = {
  id: "tldr-list",
  type: "tldr",
  config: {
    items: [
      "Approval volume rose while the pending queue narrowed.",
      "Central region carries the largest share of field activity.",
      "Flagged visits remain a small portion of the illustrated workload.",
    ],
  },
}

const singleSummary: StoryBlock = {
  id: "tldr-single",
  type: "tldr",
  config: {
    content: "Approvals are keeping pace with field activity, with the strongest volume concentrated in Central region.",
  },
}

const meta = {
  title: "Artifact System/Blocks",
  component: ArtifactGraphRenderer,
  tags: ["autodocs"],
  parameters: {
    layout: "fullscreen",
    docs: {
      description: {
        component:
          "One production Story artifact block per story. These API-independent fixtures are the visual reference for hierarchy, spacing, responsive behavior, and accessibility. Semantic Query is intentionally absent because it is a hidden compute block with no visual surface.",
      },
    },
  },
  args: {
    artifact: blockArtifact("title", "Title block", [sourceBlock("title")]),
    workspaceId: "storybook-artifact-workspace",
  },
  decorators: [
    (Story) => (
      <div className="min-h-[32rem] bg-background text-foreground">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ArtifactGraphRenderer>

export default meta
type Story = StoryObj<typeof meta>

export const TitleBlock: Story = {}

export const SectionBlock: Story = {
  args: {
    artifact: blockArtifact("section", "Section block", [sourceBlock("trend-section")]),
  },
}

export const QuestionBlock: Story = {
  args: {
    artifact: blockArtifact("question", "Question block", [sourceBlock("question")]),
  },
}

export const TldrTakeawaysBlock: Story = {
  name: "TL;DR — takeaways",
  args: {
    artifact: blockArtifact("tldr-takeaways", "TL;DR takeaways block", [listSummary]),
  },
}

export const TldrSingleBlock: Story = {
  name: "TL;DR — single summary",
  args: {
    artifact: blockArtifact("tldr-single", "TL;DR single summary block", [singleSummary]),
  },
}

export const MarkdownBlock: Story = {
  args: {
    artifact: blockArtifact("markdown", "Markdown block", [sourceBlock("provenance-note")]),
  },
}

export const DateFilterBlock: Story = {
  args: {
    artifact: blockArtifact("date-filter", "Date filter block", [sourceBlock("date-filter")]),
  },
}

export const PeriodSelectorBlock: Story = {
  args: {
    artifact: blockArtifact("period-selector", "Period selector block", [sourceBlock("period-selector")]),
  },
}

export const SemanticQueryBlock: Story = {
  name: "Semantic query — hidden compute",
  render: () => (
    <div className="mx-auto max-w-5xl px-6 py-6">
      <section className="grid gap-3 border-t border-border pt-5 sm:grid-cols-[11rem_minmax(0,1fr)] sm:gap-6">
        <h2 className="text-base font-semibold leading-6 tracking-[-0.015em]">Semantic query</h2>
        <div className="min-w-0 max-w-[70ch] space-y-3">
          <p className="text-sm leading-6 text-muted-foreground">
            This is the one registered Story block with no visual surface. It runs a verified semantic query and publishes typed rows to graph, stat, and table blocks.
          </p>
          <pre className="overflow-x-auto rounded-lg bg-muted px-4 py-3 text-xs leading-5 text-foreground"><code>{`{
  "id": "visits-query",
  "type": "semantic_query",
  "hidden": true,
  "config": {
    "queries": {
      "visits_by_day": {
        "measures": ["visits.count"],
        "time_dimension": "visits.visit_date",
        "granularity": "day"
      }
    }
  }
}`}</code></pre>
        </div>
      </section>
    </div>
  ),
}

export const StatBlock: Story = {
  args: {
    artifact: blockArtifact("stat", "Stat block", [sourceBlock("approved-stat")]),
  },
}

export const GraphBlock: Story = {
  args: {
    artifact: blockArtifact("graph", "Graph block", [sourceBlock("trend-chart")]),
  },
}

export const TableBlock: Story = {
  args: {
    artifact: blockArtifact("table", "Table block", [sourceBlock("workflow-table")]),
  },
}

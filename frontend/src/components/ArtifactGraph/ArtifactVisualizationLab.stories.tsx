import type { Meta, StoryObj } from "@storybook/react-vite"

import { ArtifactGraphRenderer } from "@/components/ArtifactGraph"
import type { ArtifactDetail } from "@/components/ArtifactGraph"
import { regionRows, trendRows } from "@/pages/ArtifactDemoPage/demoData"

import type {
  GraphCurve,
  GraphGrid,
  GraphLabels,
  GraphLegend,
  GraphOrientation,
  GraphPalette,
  StatComparisonConfig,
} from "./types"

interface VisualizationLabProps {
  palette: GraphPalette
  legend: GraphLegend
  grid: GraphGrid
  curve: GraphCurve
  orientation: GraphOrientation
  labels: GraphLabels
  comparisonType: NonNullable<StatComparisonConfig["type"]>
  pendingGoal: NonNullable<StatComparisonConfig["goal"]>
}

function VisualizationLab({
  palette,
  legend,
  grid,
  curve,
  orientation,
  labels,
  comparisonType,
  pendingGoal,
}: VisualizationLabProps) {
  const artifact: ArtifactDetail = {
    id: "artifact-visualization-lab",
    title: "Visualization lab",
    type: "story",
    code: "",
    version: 1,
    semantic_queries: [],
    data: {
      story_doc: {
        schema_version: 1,
        prd: "Interactive Storybook reference. All values are synthetic literal data.",
        blocks: [
          {
            id: "title",
            type: "title",
            config: {
              text: "Visualization lab",
              subtitle: "Use Storybook Controls to exercise Scout's production visualization grammar.",
            },
          },
          {
            id: "approved",
            type: "stat",
            row_group: "metrics",
            inputs: {
              current: { value: [{ value: 431 }] },
              previous: { value: [{ value: 396 }] },
            },
            config: {
              label: "Approved visits",
              value_key: "value",
              format: "number_0",
              comparison: {
                type: comparisonType,
                label: "vs same period last year",
                goal: "higher",
              },
            },
          },
          {
            id: "pending",
            type: "stat",
            row_group: "metrics",
            inputs: {
              current: { value: [{ value: 48 }] },
              previous: { value: [{ value: 62 }] },
            },
            config: {
              label: "Pending review",
              value_key: "value",
              format: "number_0",
              comparison: {
                type: comparisonType,
                label: "vs same period last year",
                goal: pendingGoal,
              },
            },
          },
          {
            id: "region",
            type: "graph",
            row_group: "charts",
            inputs: { data: { value: regionRows } },
            config: {
              title: "Visits by region",
              subtitle: "Category comparison · synthetic visits",
              chart_type: "bar",
              x_key: "region",
              y_format: "number_0",
              series: [
                { data_key: "approved", label: "Approved" },
                { data_key: "pending", label: "Pending" },
              ],
              style: { palette, legend, grid, orientation, labels },
              height: 300,
            },
          },
          {
            id: "trend",
            type: "graph",
            row_group: "charts",
            inputs: { data: { value: trendRows } },
            config: {
              title: "Approval trend",
              subtitle: "Weekly values · synthetic visits",
              chart_type: "line",
              x_key: "date",
              y_format: "number_0",
              series: [
                { data_key: "approved", label: "Approved" },
                { data_key: "pending", label: "Pending" },
              ],
              style: { palette, legend, grid, curve },
              height: 300,
            },
          },
        ],
      },
    },
  }

  return (
    <div className="h-screen min-h-[44rem] bg-background text-foreground">
      <ArtifactGraphRenderer artifact={artifact} workspaceId="storybook-visualization-lab" />
    </div>
  )
}

const meta = {
  title: "Artifact System/Visualization Lab",
  component: VisualizationLab,
  tags: ["autodocs"],
  parameters: {
    layout: "fullscreen",
    docs: {
      description: {
        component:
          "Interactive production-renderer lab for Scout's bounded visualization grammar. Use the Controls panel to compare named palettes, chart scaffolding, orientation, labels, and semantically styled KPI comparisons.",
      },
    },
  },
  argTypes: {
    palette: { control: "select", options: ["categorical", "status", "sequential", "monochrome"] },
    legend: { control: "select", options: ["auto", "top", "bottom", "none"] },
    grid: { control: "select", options: ["horizontal", "both", "none"] },
    curve: { control: "select", options: ["monotone", "linear", "step"] },
    orientation: { control: "select", options: ["vertical", "horizontal"] },
    labels: { control: "select", options: ["none", "value"] },
    comparisonType: { control: "select", options: ["none", "absolute", "percent"] },
    pendingGoal: { control: "select", options: ["higher", "lower", "neutral"] },
  },
  args: {
    palette: "categorical",
    legend: "top",
    grid: "horizontal",
    curve: "monotone",
    orientation: "horizontal",
    labels: "none",
    comparisonType: "percent",
    pendingGoal: "lower",
  },
} satisfies Meta<typeof VisualizationLab>

export default meta
type Story = StoryObj<typeof meta>

export const Customize: Story = {}

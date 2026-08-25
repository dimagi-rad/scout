import type { ArtifactDetail } from "@/components/ArtifactGraph"
import type { RechartsNode } from "@/components/ArtifactGraph/recharts"
import type { QueryDataResponse } from "@/components/ArtifactViewer"

export const trendRows = [
  { date: "2026-01-05", approved: 42, pending: 9, flagged: 2 },
  { date: "2026-01-12", approved: 48, pending: 8, flagged: 3 },
  { date: "2026-01-19", approved: 51, pending: 7, flagged: 1 },
  { date: "2026-01-26", approved: 57, pending: 8, flagged: 2 },
  { date: "2026-02-02", approved: 61, pending: 6, flagged: 2 },
  { date: "2026-02-09", approved: 64, pending: 5, flagged: 1 },
  { date: "2026-02-16", approved: 68, pending: 6, flagged: 2 },
  { date: "2026-02-23", approved: 73, pending: 4, flagged: 1 },
  { date: "2026-03-02", approved: 76, pending: 5, flagged: 1 },
  { date: "2026-03-09", approved: 81, pending: 4, flagged: 2 },
  { date: "2026-03-16", approved: 84, pending: 3, flagged: 1 },
  { date: "2026-03-23", approved: 88, pending: 3, flagged: 1 },
]

export const regionRows = [
  { region: "Central", approved: 118, pending: 14, flagged: 3 },
  { region: "Eastern", approved: 96, pending: 11, flagged: 5 },
  { region: "Northern", approved: 83, pending: 9, flagged: 2 },
  { region: "Southern", approved: 71, pending: 8, flagged: 4 },
  { region: "Western", approved: 63, pending: 6, flagged: 1 },
]

export const statusRows = [
  { status: "Approved", count: 431 },
  { status: "Pending", count: 48 },
  { status: "Flagged", count: 15 },
]

export const workflowRows = [
  { workflow: "Maternal health", visits: 148, completion_rate: 0.94 },
  { workflow: "Immunization", visits: 126, completion_rate: 0.88 },
  { workflow: "Nutrition", visits: 105, completion_rate: 0.91 },
  { workflow: "Follow-up", visits: 82, completion_rate: 0.84 },
]

export const scatterRows = [
  { worker: "A. Mensah", visits: 44, duration: 31 },
  { worker: "B. Kamau", visits: 52, duration: 27 },
  { worker: "C. Ncube", visits: 38, duration: 35 },
  { worker: "D. Okafor", visits: 61, duration: 24 },
  { worker: "E. Diallo", visits: 47, duration: 29 },
  { worker: "F. Banda", visits: 56, duration: 26 },
  { worker: "G. Moyo", visits: 35, duration: 38 },
]

export const storyArtifact: ArtifactDetail = {
  id: "artifact-demo-operations",
  title: "Community visit operations review",
  type: "story",
  code: "",
  data: {
    story_doc: {
      schema_version: 1,
      prd: "Illustrative data for exploring Scout artifact blocks. Values on this page are synthetic and do not represent a real program.",
      blocks: [
        {
          id: "title",
          type: "title",
          config: {
            text: "Community visit operations review",
            subtitle: "A complete Story artifact assembled from reusable narrative, data, and chart blocks.",
          },
        },
        {
          id: "question",
          type: "question",
          config: {
            text: "Are visit approvals keeping pace with field activity?",
            context: "Review status mix, regional workload, and completion rates before the weekly operations meeting.",
          },
        },
        {
          id: "summary",
          type: "tldr",
          config: {
            items: [
              "Approvals increased across the illustrated period while the pending queue narrowed.",
              "Central region carries the largest volume; flagged visits remain a small share in every region.",
              "Maternal health has the strongest completion rate in the synthetic workflow sample.",
            ],
          },
        },
        {
          id: "controls-note",
          type: "section",
          config: {
            title: "Time controls",
            body: "Date and comparison controls emit typed ranges for query blocks. In this synthetic preview the charts below intentionally use literal rows.",
          },
        },
        {
          id: "date-filter",
          type: "date_filter",
          row_group: "time-controls",
          config: { label: "Date range", default: "last_30_days" },
        },
        {
          id: "period-selector",
          type: "period_selector",
          row_group: "time-controls",
          config: {
            label: "Comparison period",
            default_range: "last_30_days",
            default_comparison: "previous_year",
          },
        },
        {
          id: "approved-stat",
          type: "stat",
          row_group: "kpis",
          inputs: {
            current: { value: [{ value: 431 }] },
            previous: { value: [{ value: 396 }] },
          },
          config: {
            label: "Approved visits",
            value_key: "value",
            format: "number",
            comparison: {
              type: "percent",
              format: "percent_0",
              label: "vs same period last year",
              goal: "higher",
            },
          },
        },
        {
          id: "pending-stat",
          type: "stat",
          row_group: "kpis",
          inputs: {
            current: { value: [{ value: 48 }] },
            previous: { value: [{ value: 62 }] },
          },
          config: {
            label: "Pending review",
            value_key: "value",
            format: "number",
            comparison: {
              type: "percent",
              format: "percent_0",
              label: "vs same period last year",
              goal: "lower",
            },
          },
        },
        {
          id: "completion-stat",
          type: "stat",
          row_group: "kpis",
          inputs: {
            current: { value: [{ value: 0.9 }] },
            previous: { value: [{ value: 0.86 }] },
          },
          config: {
            label: "Completion rate",
            value_key: "value",
            format: "percent_0",
            comparison: { type: "absolute", label: "vs same period last year", goal: "higher" },
          },
        },
        {
          id: "payment-stat",
          type: "stat",
          row_group: "kpis",
          inputs: {
            current: { value: [{ value: 18240 }] },
            previous: { value: [{ value: 16650 }] },
          },
          config: {
            label: "Payment accrued",
            value_key: "value",
            format: "currency_0",
            comparison: { type: "absolute", label: "vs same period last year", goal: "neutral" },
          },
        },
        {
          id: "trend-section",
          type: "section",
          config: {
            title: "Approval trend",
            body: "The line chart and table below share the same typed rows. Hover the chart to inspect exact values.",
          },
        },
        {
          id: "trend-chart",
          type: "graph",
          inputs: { data: { value: trendRows } },
          config: {
            title: "Approved and pending visits",
            subtitle: "Weekly visits · synthetic demo data",
            chart_type: "line",
            x_key: "date",
            series: [
              { data_key: "approved", label: "Approved" },
              { data_key: "pending", label: "Pending" },
            ],
            style: { palette: "categorical", legend: "top", grid: "horizontal", curve: "monotone" },
            height: 300,
            y_format: "number",
          },
        },
        {
          id: "region-chart",
          type: "graph",
          row_group: "regional",
          inputs: { data: { value: regionRows } },
          config: {
            title: "Visits by region",
            chart_type: "bar",
            x_key: "region",
            series: [
              { data_key: "approved", label: "Approved" },
              { data_key: "pending", label: "Pending" },
              { data_key: "flagged", label: "Flagged" },
            ],
            stacked: true,
            style: { palette: "status", legend: "top", grid: "horizontal", orientation: "horizontal" },
            height: 260,
          },
        },
        {
          id: "status-chart",
          type: "graph",
          row_group: "regional",
          inputs: { data: { value: statusRows } },
          config: {
            title: "Review status",
            chart_type: "donut",
            x_key: "status",
            y_key: "count",
            data_label: "Visits",
            style: { palette: "status", legend: "bottom", labels: "none" },
            height: 260,
          },
        },
        {
          id: "workflow-section",
          type: "section",
          config: {
            title: "Workflow detail",
            body: "Tables can carry the audit-friendly detail behind a visual summary and preserve formatting for measures.",
          },
        },
        {
          id: "workflow-table",
          type: "table",
          inputs: { data: { value: workflowRows } },
          config: {
            columns: [
              { key: "workflow", label: "Workflow" },
              { key: "visits", label: "Visits", format: "number" },
              { key: "completion_rate", label: "Completion rate", format: "percent_0" },
            ],
          },
        },
        {
          id: "provenance-note",
          type: "markdown",
          config: {
            body: "**Reproducibility:** Story artifacts retain their semantic query definitions, result columns, version, and generation manifest. Use **View Data** above to inspect the demo contract.",
          },
        },
      ],
    },
  },
  semantic_queries: [],
  semantic_query_manifest: {
    generated_at: "2026-08-25T11:30:00Z",
    entries: [
      { query_key: "visits_by_day" },
      { query_key: "visits_by_status" },
      { query_key: "workflow_completion" },
    ],
  },
  version: 4,
}

export const chartTrees: Record<string, RechartsNode> = {
  line: {
    type: "LineChart",
    children: [
      { type: "CartesianGrid", props: { vertical: false, stroke: "var(--border)" } },
      { type: "XAxis", props: { dataKey: "date", tickLine: false, axisLine: false } },
      { type: "YAxis", props: { tickLine: false, axisLine: false } },
      { type: "Tooltip", props: { formatter: { $format: "number" } } },
      { type: "Legend" },
      { type: "Line", props: { dataKey: "approved", name: "Approved", stroke: "var(--chart-1)", strokeWidth: 2.5 } },
      { type: "Line", props: { dataKey: "pending", name: "Pending", stroke: "var(--chart-2)", strokeWidth: 2.5 } },
    ],
  },
  bar: {
    type: "BarChart",
    children: [
      { type: "CartesianGrid", props: { vertical: false, stroke: "var(--border)" } },
      { type: "XAxis", props: { dataKey: "region", tickLine: false, axisLine: false } },
      { type: "YAxis", props: { tickLine: false, axisLine: false } },
      { type: "Tooltip", props: { formatter: { $format: "number" } } },
      { type: "Legend" },
      { type: "Bar", props: { dataKey: "approved", name: "Approved", fill: "var(--chart-1)", stackId: "status" } },
      { type: "Bar", props: { dataKey: "pending", name: "Pending", fill: "var(--chart-warning)", stackId: "status" } },
      { type: "Bar", props: { dataKey: "flagged", name: "Flagged", fill: "var(--destructive)", stackId: "status" } },
    ],
  },
  area: {
    type: "AreaChart",
    children: [
      { type: "CartesianGrid", props: { vertical: false, stroke: "var(--border)" } },
      { type: "XAxis", props: { dataKey: "date", tickLine: false, axisLine: false } },
      { type: "YAxis", props: { tickLine: false, axisLine: false } },
      { type: "Tooltip", props: { formatter: { $format: "number" } } },
      { type: "Area", props: { type: "monotone", dataKey: "approved", name: "Approved", stroke: "var(--chart-positive)", fill: "var(--chart-positive)", fillOpacity: 0.18 } },
    ],
  },
  pie: {
    type: "PieChart",
    children: [
      { type: "Tooltip", props: { formatter: { $format: "number" } } },
      { type: "Pie", props: { dataKey: "count", nameKey: "status", innerRadius: "48%", outerRadius: "78%", paddingAngle: 2 } },
      { type: "Legend" },
    ],
  },
  scatter: {
    type: "ScatterChart",
    children: [
      { type: "CartesianGrid", props: { stroke: "var(--border)" } },
      { type: "XAxis", props: { type: "number", dataKey: "visits", name: "Visits", tickLine: false } },
      { type: "YAxis", props: { type: "number", dataKey: "duration", name: "Minutes", tickLine: false } },
      { type: "Tooltip" },
      { type: "Scatter", props: { name: "Field workers", fill: "var(--chart-4)" } },
    ],
  },
  composed: {
    type: "ComposedChart",
    children: [
      { type: "CartesianGrid", props: { vertical: false, stroke: "var(--border)" } },
      { type: "XAxis", props: { dataKey: "workflow", tickLine: false, axisLine: false } },
      { type: "YAxis", props: { yAxisId: "visits", tickLine: false, axisLine: false } },
      { type: "YAxis", props: { yAxisId: "rate", orientation: "right", domain: [0, 1], tickFormatter: { $format: "percent_0" }, tickLine: false, axisLine: false } },
      { type: "Tooltip" },
      { type: "Legend" },
      { type: "Bar", props: { dataKey: "visits", name: "Visits", fill: "var(--chart-3)", yAxisId: "visits" } },
      { type: "Line", props: { dataKey: "completion_rate", name: "Completion rate", stroke: "var(--chart-5)", yAxisId: "rate", strokeWidth: 2.5 } },
      { type: "ReferenceLine", props: { y: 0.9, yAxisId: "rate", stroke: "var(--chart-warning)", strokeDasharray: "4 4", label: "90% target" } },
    ],
  },
}

export function demoQueryData(refreshCount = 0): QueryDataResponse {
  const approvedBoost = refreshCount % 3
  return {
    queries: [
      {
        name: "visits_by_status",
        semantic_query: {
          measures: ["visits.count"],
          dimensions: ["visits.status"],
          filters: [{ field: "visits.completed", operator: "equals", value: true }],
          order_by: [{ field: "visits.count", direction: "desc" }],
        },
        columns: ["status", "visits_count"],
        rows: [
          ["approved", 431 + approvedBoost],
          ["pending", 48],
          ["flagged", 15],
        ],
        row_count: 3,
      },
      {
        name: "workflow_completion",
        semantic_query: {
          measures: ["visits.count", "visits.completion_rate"],
          dimensions: ["visits.workflow"],
        },
        columns: ["workflow", "visits_count", "completion_rate"],
        rows: workflowRows.map((row) => [row.workflow, row.visits, row.completion_rate]),
        row_count: workflowRows.length,
      },
    ],
    static_data: {
      title: "Community visit operations review",
      notes: "Synthetic data for interface demonstration",
    },
    semantic_query_manifest: storyArtifact.semantic_query_manifest,
  }
}

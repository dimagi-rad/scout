import { StrictMode } from "react"
import { render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"

import { ArtifactGraphRenderer } from "./ArtifactGraphRenderer"
import { buildSemanticQueryInput } from "./runtime"
import type { ArtifactDetail } from "./types"

vi.mock("@/api/client", () => ({
  api: {
    post: vi.fn(),
  },
}))

const mockedPost = vi.mocked(api.post)

function artifact(): ArtifactDetail {
  return {
    id: "artifact-1",
    title: "Visits",
    type: "story",
    code: "",
    data: {
      story_doc: {
        schema_version: 1,
        prd: "Shows visits over time.",
        blocks: [
          { id: "title", type: "title", config: { text: "Visits" } },
          { id: "range", type: "date_filter", config: { default: "last_7_days" } },
          {
            id: "q",
            type: "semantic_query",
            hidden: true,
            inputs: { date_range: { $ref: "range.value" } },
            config: {
              queries: {
                visits_by_day: {
                  measures: ["visits.count"],
                  time_dimension: "visits.visit_date",
                  granularity: "day",
                },
              },
            },
          },
          {
            id: "chart",
            type: "graph",
            inputs: { data: { $ref: "q.visits_by_day" } },
            config: {
              title: "Visits by day",
              chart_type: "line",
              x_key: "date",
              series: ["visits_count"],
            },
          },
          {
            id: "table",
            type: "table",
            inputs: { data: { $ref: "q.visits_by_day" } },
            config: { columns: ["date", "visits_count"] },
          },
          {
            id: "stat",
            type: "stat",
            inputs: { current: { $ref: "q.visits_by_day" } },
            config: { label: "Total visits", value_key: "visits_count", value_path: "visits_count" },
          },
        ],
      },
    },
    semantic_queries: [],
    version: 1,
  }
}

describe("ArtifactGraphRenderer", () => {
  beforeEach(() => {
    mockedPost.mockReset()
  })

  it("renders visible blocks fed by a hidden semantic query block", async () => {
    mockedPost.mockResolvedValue({
      columns: ["date", "visits__count"],
      rows: [["2026-06-24", 12]],
      row_count: 1,
    })

    const { container } = render(<ArtifactGraphRenderer artifact={artifact()} workspaceId="workspace-1" />)

    expect(screen.getByRole("heading", { name: "Visits" })).toBeInTheDocument()
    expect(screen.getByText("Visits by day")).toBeInTheDocument()
    await waitFor(() => expect(container.querySelector('[data-block-type="graph"]')).toBeInTheDocument())
    expect(await screen.findAllByText("visits_count")).toHaveLength(1)
    expect(screen.getAllByText("2026-06-24").length).toBeGreaterThan(0)
    expect(screen.getByText("Total visits")).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText("12").length).toBeGreaterThan(0))
    expect(mockedPost).toHaveBeenCalledWith(
      "/api/workspaces/workspace-1/semantic-query/",
      expect.objectContaining({
        measures: ["visits.count"],
        time_dimension: "visits.visit_date",
        granularity: "day",
      }),
    )
  })

  it("rejects Recharts props.data refs instead of falling back to block rows", async () => {
    const invalidArtifact = artifact()
    invalidArtifact.data.story_doc = {
      schema_version: 1,
      blocks: [
        {
          id: "pie",
          type: "graph",
          inputs: {
            data: {
              value: [
                { status: "Approved", visits_count: 12 },
                { status: "Pending", visits_count: 3 },
              ],
            },
          },
          config: {
            title: "Visit status",
            recharts: {
              type: "PieChart",
              children: [
                {
                  type: "Pie",
                  props: {
                    data: { $ref: "q.status" },
                    dataKey: "visits_count",
                    nameKey: "status",
                  },
                },
              ],
            },
          },
        },
      ],
    }

    render(<ArtifactGraphRenderer artifact={invalidArtifact} workspaceId="workspace-1" />)

    expect(await screen.findByText(/Chart config error/)).toBeInTheDocument()
    expect(screen.getByText(/Recharts Pie prop "data" is not supported/)).toBeInTheDocument()
    expect(mockedPost).not.toHaveBeenCalled()
  })

  it("lays adjacent row_group blocks out as a responsive row", () => {
    const kpiArtifact = artifact()
    kpiArtifact.data.story_doc = {
      schema_version: 1,
      blocks: [
        { id: "title", type: "title", config: { text: "Visit KPIs" } },
        {
          id: "verified",
          type: "stat",
          row_group: "kpis",
          inputs: { current: { value: [{ value: 73 }] } },
          config: { label: "Verified visits", value_key: "value" },
        },
        {
          id: "pending",
          type: "stat",
          row_group: "kpis",
          inputs: { current: { value: [{ value: 2 }] } },
          config: { label: "Pending visits", value_key: "value" },
        },
        {
          id: "flagged",
          type: "stat",
          row_group: "kpis",
          inputs: { current: { value: [{ value: 2 }] } },
          config: { label: "Flagged visits", value_key: "value" },
        },
        {
          id: "payment",
          type: "stat",
          row_group: "kpis",
          inputs: { current: { value: [{ value: 361 }] } },
          config: { label: "Total payment accrued", value_key: "value", format: "currency_2" },
        },
      ],
    }

    const { container } = render(<ArtifactGraphRenderer artifact={kpiArtifact} workspaceId="workspace-1" />)
    const row = container.querySelector<HTMLElement>('[data-block-row-group="kpis"]')
    const statBlocks = row?.querySelectorAll('[data-block-type="stat"]') ?? []

    expect(row).toBeInTheDocument()
    expect(row).toHaveStyle({
      gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 210px), 1fr))",
    })
    expect(statBlocks).toHaveLength(4)
    expect(screen.getByRole("heading", { name: "Key metrics" })).toBeInTheDocument()
    expect(screen.getByText("$361.00")).toBeInTheDocument()
    expect(row?.className).not.toContain("data-stat-period]]:hidden")
    expect(mockedPost).not.toHaveBeenCalled()
  })

  it("renders list and single-summary TLDR blocks as labeled high-emphasis regions", () => {
    const summaryArtifact = artifact()
    summaryArtifact.data.story_doc = {
      schema_version: 1,
      blocks: [
        {
          id: "takeaways",
          type: "tldr",
          config: { items: ["Approvals rose.", "Pending work fell."] },
        },
        {
          id: "headline",
          type: "tldr",
          config: { content: "Operations are keeping pace with demand." },
        },
      ],
    }

    const { container } = render(<ArtifactGraphRenderer artifact={summaryArtifact} workspaceId="workspace-1" />)

    expect(screen.getAllByRole("heading", { name: "In brief" })).toHaveLength(2)
    expect(screen.getByText("Approvals rose.")).toBeInTheDocument()
    expect(screen.getByText("Operations are keeping pace with demand.")).toBeInTheDocument()
    expect(container.querySelectorAll('[data-block-type="tldr"]')).toHaveLength(2)
  })

  it("only consolidates comparison captions when every grouped KPI shares an active comparison", () => {
    const kpiArtifact = artifact()
    kpiArtifact.data.story_doc = {
      schema_version: 1,
      blocks: [
        {
          id: "approved",
          type: "stat",
          row_group: "kpis",
          inputs: {
            current: { value: [{ value: 8 }] },
            previous: { value: [{ value: 6 }] },
          },
          config: {
            label: "Approved",
            value_key: "value",
            comparison: { type: "absolute", label: "vs previous period" },
          },
        },
        {
          id: "pending",
          type: "stat",
          row_group: "kpis",
          inputs: {
            current: { value: [{ value: 2 }] },
            previous: { value: [{ value: 3 }] },
          },
          config: {
            label: "Pending",
            value_key: "value",
            comparison: { type: "absolute", label: "vs previous year" },
          },
        },
      ],
    }

    const { container } = render(<ArtifactGraphRenderer artifact={kpiArtifact} workspaceId="workspace-1" />)
    const row = container.querySelector<HTMLElement>('[data-block-row-group="kpis"]')

    expect(row?.className).not.toContain("data-stat-period]]:hidden")
    expect(screen.getByText(/vs previous period/)).toBeInTheDocument()
    expect(screen.getByText(/vs previous year/)).toBeInTheDocument()
    expect(screen.queryByText(/Compared with/)).not.toBeInTheDocument()
  })

  it("renders literal graph inputs inside React Strict Mode", async () => {
    const literalArtifact = artifact()
    literalArtifact.data.story_doc = {
      schema_version: 1,
      blocks: [
        {
          id: "chart",
          type: "graph",
          inputs: {
            data: {
              value: [
                { date: "2026-06-01", visits: 12 },
                { date: "2026-06-02", visits: 15 },
              ],
            },
          },
          config: {
            chart_type: "line",
            x_key: "date",
            y_key: "visits",
          },
        },
      ],
    }

    const { container } = render(
      <StrictMode>
        <ArtifactGraphRenderer artifact={literalArtifact} workspaceId="workspace-1" />
      </StrictMode>,
    )

    await waitFor(() => {
      expect(container.querySelector('[data-block-type="graph"]')).toBeInTheDocument()
    })
    expect(screen.queryByText("Loading data...")).not.toBeInTheDocument()
    expect(mockedPost).not.toHaveBeenCalled()
  })

  it("turns date_range into an inDateRange semantic filter", () => {
    const query = buildSemanticQueryInput({
      measures: ["visits.count"],
      time_dimension: "visits.visit_date",
      date_range: { start: "2026-06-01", end: "2026-06-30" },
    })

    expect(query.filters).toContainEqual({
      field: "visits.visit_date",
      operator: "inDateRange",
      values: ["2026-06-01", "2026-06-30"],
    })
  })
})

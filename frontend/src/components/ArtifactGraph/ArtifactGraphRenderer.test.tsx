import { render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

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
            config: { label: "Total visits", value_key: "visits_count" },
          },
        ],
      },
    },
    semantic_queries: [],
    version: 1,
  }
}

describe("ArtifactGraphRenderer", () => {
  it("renders visible blocks fed by a hidden semantic query block", async () => {
    mockedPost.mockResolvedValue({
      columns: ["date", "visits__count"],
      rows: [["2026-06-24", 12]],
      row_count: 1,
    })

    render(<ArtifactGraphRenderer artifact={artifact()} workspaceId="workspace-1" />)

    expect(screen.getByRole("heading", { name: "Visits" })).toBeInTheDocument()
    expect(screen.getByText("Visits by day")).toBeInTheDocument()
    expect(await screen.findAllByText("visits_count")).toHaveLength(2)
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

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { api } from "@/api/client"

import { ArtifactGraphRenderer } from "./ArtifactGraphRenderer"
import type { ArtifactDetail } from "./types"

vi.mock("@/api/client", () => ({ api: { post: vi.fn() } }))
const post = vi.mocked(api.post)

afterEach(() => {
  post.mockReset()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it("filters categorical chart/table rows without projecting the time field, including after recovery", async () => {
  vi.useFakeTimers({ toFake: ["Date"] })
  vi.setSystemTime(new Date("2026-09-15T12:00:00Z"))
  // jsdom has no layout engine; give the real Recharts renderer a viewport.
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0, y: 0, width: 800, height: 280,
    top: 0, left: 0, right: 800, bottom: 280, toJSON: () => ({}),
  })
  post.mockResolvedValue({ columns: ["topics.topic", "topics.count"], rows: [["Account update", 2]], row_count: 1 })
  const artifact: ArtifactDetail = {
    id: "filter-only-artifact", title: "Topics", type: "story", code: "", version: 1, semantic_queries: [],
    data: { story_doc: { schema_version: 1, blocks: [
      { id: "period", type: "date_filter", config: { default: "last_30_days" } },
      { id: "q", type: "semantic_query", hidden: true, inputs: { date_range: { $ref: "period.value" } }, config: {
        queries: { topics: { measures: ["topics.count"], dimensions: ["topics.topic"], time_dimension: "topics.created_at" } },
      } },
      { id: "chart", type: "graph", inputs: { data: { $ref: "q.topics" } }, config: { chart_type: "bar", x_key: "topics_topic", series: ["topics_count"] } },
      { id: "table", type: "table", inputs: { data: { $ref: "q.topics" } }, config: { columns: ["topics_topic", "topics_count"] } },
    ] } },
  }
  const { container, rerender } = render(<ArtifactGraphRenderer artifact={artifact} workspaceId="workspace" dataRevision="before" />)
  await waitFor(() => expect(within(screen.getByRole("table")).getByText("2")).toBeInTheDocument())
  const chart = container.querySelector('[data-block-type="graph"]')
  expect(chart?.querySelector("svg")).toBeInTheDocument()
  expect(post).toHaveBeenCalledTimes(1)
  expect(post).toHaveBeenLastCalledWith("/api/workspaces/workspace/semantic-query/", expect.objectContaining({
    measures: ["topics.count"], dimensions: ["topics.topic"], time_dimension: "topics.created_at", granularity: undefined,
    filters: [{ field: "topics.created_at", operator: "inDateRange", values: ["2026-08-17", "2026-09-15"] }],
  }))
  expect(screen.queryByText("topics_created_at")).not.toBeInTheDocument()

  post.mockResolvedValue({ columns: ["topics.topic", "topics.count"], rows: [["Account update", 1]], row_count: 1 })
  fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-09-13" } })
  await waitFor(() => expect(within(screen.getByRole("table")).getByText("1")).toBeInTheDocument())
  expect(post).toHaveBeenCalledTimes(2)
  expect(post).toHaveBeenLastCalledWith("/api/workspaces/workspace/semantic-query/", expect.objectContaining({
    dimensions: ["topics.topic"], time_dimension: "topics.created_at", granularity: undefined,
    filters: [{ field: "topics.created_at", operator: "inDateRange", values: ["2026-09-13", "2026-09-15"] }],
  }))

  post.mockResolvedValue({ columns: ["topics.topic", "topics.count"], rows: [["Account update", 3]], row_count: 1 })
  rerender(<ArtifactGraphRenderer artifact={artifact} workspaceId="workspace" dataRevision="after" />)
  await waitFor(() => expect(within(screen.getByRole("table")).getByText("3")).toBeInTheDocument())
  expect(post).toHaveBeenCalledTimes(3)
  expect(screen.getByLabelText("Start date")).toHaveValue("2026-09-13")
  expect(post).toHaveBeenLastCalledWith("/api/workspaces/workspace/semantic-query/", expect.objectContaining({
    filters: [{ field: "topics.created_at", operator: "inDateRange", values: ["2026-09-13", "2026-09-15"] }],
  }))
  expect(container.querySelector('[data-block-type="graph"]')).toBe(chart)
  expect(screen.queryByText(/Loading data|Waiting on|failed:/)).not.toBeInTheDocument()
})

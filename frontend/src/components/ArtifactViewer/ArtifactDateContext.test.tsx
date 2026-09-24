import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import type { ArtifactDetail, ArtifactQueryContext } from "@/components/ArtifactGraph/types"
import { ArtifactViewer } from "./ArtifactViewer"
import type { QueryDataResponse } from "./types"
import { useArtifactDateSources, useArtifactQueryData } from "./useArtifactQueryData"

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

it("does not carry removed date-control ids into another artifact revision", () => {
  const artifact: ArtifactDetail = { id: "artifact", title: "Test", type: "story", code: "", data: {}, semantic_queries: [], version: 1 }
  const { result, rerender } = renderHook(({ value }) => useArtifactDateSources(value), { initialProps: { value: artifact } })
  const staleUpdate = result.current[1]
  act(() => result.current[1]({ removed: { start: "2026-09-01", end: "2026-09-02" } }))
  expect(result.current[0]).toHaveProperty("removed")
  rerender({ value: { ...artifact, version: 2 } })
  expect(result.current[0]).toEqual({})
  act(() => staleUpdate({ removed: { start: "2026-09-01", end: "2026-09-02" } }))
  expect(result.current[0]).toEqual({})
})

const context = {
  as_of: "2026-09-16T00:30:00Z", timezone: "Asia/Singapore", today: "2026-09-16",
  presets: {
    last_30_days: { start: "2026-08-18", end: "2026-09-16", preset: "last_30_days", comparisons: {
      previous_period: { start: "2026-07-19", end: "2026-08-17" }, previous_year: { start: "2025-08-18", end: "2025-09-16" },
    } },
    last_7_days: { start: "2026-09-10", end: "2026-09-16", preset: "last_7_days", comparisons: {
      previous_period: { start: "2026-09-03", end: "2026-09-09" }, previous_year: { start: "2025-09-10", end: "2025-09-16" },
    } },
  },
}

it("uses the server clock and carries changed chart dates into View Data", async () => {
  vi.useFakeTimers({ toFake: ["Date"] })
  vi.setSystemTime(new Date("2035-01-01T12:00:00Z"))
  const artifact: ArtifactDetail = {
    id: "artifact", title: "Sessions", type: "story", code: "", version: 1,
    semantic_queries: [], date_context: context,
    data: { story_doc: { schema_version: 1, blocks: [
      { id: "range", type: "date_filter", config: { label: "Activity window", default: "last_30_days" } },
      { id: "q", type: "semantic_query", hidden: true, inputs: { date_range: { $ref: "range.value" } }, config: {
        queries: { sessions: { measures: ["sessions.count"], time_dimension: "sessions.created_at" } },
      } },
      { id: "table", type: "table", inputs: { data: { $ref: "q.sessions" } }, config: { columns: ["sessions_count"] } },
    ] } },
  }
  vi.spyOn(api, "get").mockResolvedValue(artifact)
  const post = vi.spyOn(api, "post").mockImplementation(async (url, body) => {
    if (url.endsWith("/query-data/")) {
      const runtime = body as ArtifactQueryContext
      const value = runtime.sources.range.start === "2026-09-10" ? 5 : 18
      return { queries: [{ name: "q.sessions", columns: ["sessions.count"], rows: [[value]], row_count: 1 }], static_data: {} }
    }
    const query = body as { filters: Array<{ values: string[] }> }
    return { columns: ["sessions.count"], rows: [[query.filters[0].values[0] === "2026-09-10" ? 5 : 18]], row_count: 1 }
  })
  render(<ArtifactViewer artifactId="artifact" workspaceId="workspace" />)
  await screen.findByText("18")
  expect(screen.getByLabelText("Start date")).toHaveValue("2026-08-18")
  expect(screen.getByText(/Reporting timezone: Asia\/Singapore/)).toBeInTheDocument()
  expect(post).toHaveBeenCalledWith("/api/workspaces/workspace/semantic-query/", expect.objectContaining({
    query_context: { as_of: context.as_of, timezone: "Asia/Singapore" },
  }))
  fireEvent.click(screen.getByRole("button", { name: "View Data" }))
  await waitFor(() => expect(within(screen.getByRole("dialog")).getByText("18")).toBeInTheDocument())
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Close" }))
  fireEvent.change(screen.getByRole("combobox", { name: "Activity window" }), { target: { value: "last_7_days" } })
  await screen.findByText("5")
  fireEvent.click(screen.getByRole("button", { name: "View Data" }))
  await waitFor(() => expect(within(screen.getByRole("dialog")).getByText("5")).toBeInTheDocument())
  expect(post).toHaveBeenLastCalledWith("/api/workspaces/workspace/artifacts/artifact/query-data/", {
    as_of: context.as_of, timezone: "Asia/Singapore", sources: { range: { start: "2026-09-10", end: "2026-09-16" } },
  })
})

it.each(["previous_period", "previous_year"] as const)("keeps %s queries aligned when the comparison window changes", async (comparison) => {
  const artifact: ArtifactDetail = {
    id: "artifact", title: "Compared sessions", type: "story", code: "", version: 1,
    semantic_queries: [], date_context: context,
    data: { story_doc: { schema_version: 1, blocks: [
      { id: "range", type: "period_selector", config: { default_range: "last_30_days", default_comparison: comparison } },
      { id: "q", type: "semantic_query", hidden: true, inputs: { compare: { $ref: "range.pair" } }, config: {
        compare: true, queries: { sessions: { measures: ["sessions.count"], time_dimension: "sessions.created_at" } },
      } },
      { id: "table", type: "table", inputs: { data: { $ref: "q.sessions" } }, config: { columns: ["sessions_count"] } },
    ] } },
  }
  vi.spyOn(api, "get").mockResolvedValue(artifact)
  const post = vi.spyOn(api, "post").mockImplementation(async (url) => url.endsWith("/query-data/")
    ? { queries: [], static_data: {} }
    : { columns: ["sessions.count"], rows: [[18]], row_count: 1 })
  render(<ArtifactViewer artifactId="artifact" workspaceId="workspace" />)
  await screen.findByText("18")
  post.mockClear()
  fireEvent.change(screen.getByRole("combobox", { name: "Comparison period" }), { target: { value: "last_7_days" } })
  await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
  const current = context.presets.last_7_days
  const previous = current.comparisons[comparison]
  const queryUrl = "/api/workspaces/workspace/semantic-query/"
  for (const range of [current, previous]) {
    expect(post).toHaveBeenCalledWith(queryUrl, expect.objectContaining({
      filters: [{ field: "sessions.created_at", operator: "inDateRange", values: [range.start, range.end] }],
    }))
  }
  fireEvent.click(screen.getByRole("button", { name: "View Data" }))
  await waitFor(() => expect(post).toHaveBeenLastCalledWith(
    "/api/workspaces/workspace/artifacts/artifact/query-data/",
    { as_of: context.as_of, timezone: context.timezone, sources: { range: { start: current.start, end: current.end } } },
  ))
})

it("replaces an in-flight inspector request when dates change", async () => {
  let finishOld!: (data: QueryDataResponse) => void
  vi.spyOn(api, "post").mockReturnValueOnce(new Promise(resolve => { finishOld = resolve }))
    .mockResolvedValueOnce({ queries: [{ name: "new", rows: [[5]] }], static_data: {} })
  const initial: ArtifactQueryContext = { sources: { range: { start: "2026-08-18", end: "2026-09-16" } } }
  const { result, rerender } = renderHook(({ runtime }) => useArtifactQueryData("artifact", "workspace", runtime), { initialProps: { runtime: initial } })
  let oldRequest!: Promise<void>
  act(() => { oldRequest = result.current.refetch() })
  rerender({ runtime: { sources: { range: { start: "2026-09-10", end: "2026-09-16" } } } })
  await waitFor(() => expect(result.current.queryData?.queries[0].rows).toEqual([[5]]))
  await act(async () => { finishOld({ queries: [{ name: "old", rows: [[18]] }], static_data: {} }); await oldRequest })
  expect(result.current.queryData?.queries[0].rows).toEqual([[5]])
})

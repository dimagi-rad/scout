import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ArtifactGraphRenderer } from "./ArtifactGraphRenderer"
import type { ArtifactDetail } from "./types"

afterEach(() => vi.unstubAllGlobals())

describe("artifact semantic-query error presentation", () => {
  it.each(["date_filter", "period_selector"])("blocks dependent queries when a selected %s preset is missing from the server clock", async (type) => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({
      columns: ["visits.count"], rows: [[3]], row_count: 1,
    }), { status: 200 }))
    vi.stubGlobal("fetch", fetchMock)
    const onDateSourcesChange = vi.fn()
    const artifact: ArtifactDetail = {
      id: "partial-clock", title: "Readable artifact", type: "story", code: "", version: 1,
      semantic_queries: [],
      date_context: {
        as_of: "2026-09-24T12:00:00Z", today: "2026-09-24", timezone: "UTC",
        presets: { last_30_days: { start: "2026-08-26", end: "2026-09-24", comparisons: {} } },
      },
      data: { story_doc: { blocks: [
        { id: "title", type: "title", config: { text: "Readable artifact" } },
        { id: "range", type, config: {} },
        { id: "query", type: "semantic_query", hidden: true,
          inputs: { date_range: { $ref: `range.${type === "date_filter" ? "value" : "current"}` } },
          config: { queries: { visits: { measures: ["visits.count"], time_dimension: "visits.date" } } },
        },
        { id: "table", type: "table", inputs: { data: { $ref: "query.visits" } }, config: { columns: ["visits_count"] } },
      ] } },
    }
    render(<ArtifactGraphRenderer artifact={artifact} workspaceId="workspace-1" onDateSourcesChange={onDateSourcesChange} />)
    const control = await screen.findByRole("combobox")
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    fireEvent.change(control, { target: { value: "last_7_days" } })
    expect(await screen.findByTestId("artifact-date-control-error")).toHaveTextContent("Unsupported date preset: last_7_days")
    expect(screen.getByRole("heading", { name: "Readable artifact" })).toBeInTheDocument()
    expect(onDateSourcesChange).toHaveBeenLastCalledWith({})
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each(["date_filter", "period_selector"])("keeps the artifact readable when a saved %s preset is invalid", async (type) => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)
    const artifact: ArtifactDetail = {
      id: "bad-date-artifact", title: "Readable artifact", type: "story", code: "", version: 1,
      semantic_queries: [],
      data: { story_doc: { blocks: [
        { id: "title", type: "title", config: { text: "Readable artifact" } },
        { id: "range", type, config: { default: "last_60_days", default_range: "last_60_days" } },
        { id: "query", type: "semantic_query", hidden: true,
          inputs: { date_range: { $ref: `range.${type === "date_filter" ? "value" : "current"}` } },
          config: { queries: { visits: { measures: ["visits.count"], time_dimension: "visits.date" } } },
        },
        { id: "table", type: "table", inputs: { data: { $ref: "query.visits" } }, config: { columns: ["visits_count"] } },
        { id: "context", type: "section", config: { title: "Context", body: "Unaffected narrative remains readable." } },
      ] } },
    }
    render(<ArtifactGraphRenderer artifact={artifact} workspaceId="workspace-1" />)
    expect(await screen.findByTestId("artifact-date-control-error")).toHaveTextContent("Unsupported date preset")
    expect(screen.getByRole("heading", { name: "Readable artifact" })).toBeInTheDocument()
    expect(screen.getByText("Unaffected narrative remains readable.")).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("shows the actual API explanation through the real client and graph engine", async () => {
    const message = "Unknown semantic field 'visits.missing_measure'."
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      success: false,
      error: { code: "VALIDATION_ERROR", message, detail: "private diagnostic" },
    }), { status: 400, statusText: "Bad Request" }))
    vi.stubGlobal("fetch", fetchMock)
    const artifact: ArtifactDetail = {
      id: "validation-artifact",
      title: "Visits",
      type: "story",
      code: "",
      data: {
        story_doc: {
          schema_version: 1,
          blocks: [
            { id: "title", type: "title", config: { text: "Visits" } },
            {
              id: "query", type: "semantic_query", hidden: true,
              config: { queries: { visits: { measures: ["visits.missing_measure"] } } },
            },
            {
              id: "table", type: "table", inputs: { data: { $ref: "query.visits" } },
              config: { columns: ["visits_missing_measure"] },
            },
            {
              id: "summary", type: "section",
              config: { title: "Context", text: "This explanation remains readable when a query fails." },
            },
          ],
        },
      },
      semantic_queries: [],
      version: 1,
    }
    const { container } = render(<ArtifactGraphRenderer artifact={artifact} workspaceId="workspace-1" />)

    expect(await screen.findByText(new RegExp("Unknown semantic field"))).toHaveTextContent(message)
    expect(screen.getByRole("heading", { name: "Visits" })).toBeInTheDocument()
    expect(screen.getByText("This explanation remains readable when a query fails.")).toBeInTheDocument()
    expect(container).not.toHaveTextContent("[object Object]")
    expect(container).not.toHaveTextContent("private diagnostic")
    expect(container).not.toHaveTextContent("Loading data")
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith("/api/workspaces/workspace-1/semantic-query/", expect.objectContaining({
      method: "POST", credentials: "include",
    }))
  })
})

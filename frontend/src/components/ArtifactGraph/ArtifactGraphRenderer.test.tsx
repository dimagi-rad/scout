import { StrictMode } from "react"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

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

  it("refreshes published data without resetting the selected custom period or renderer DOM", async () => {
    mockedPost.mockResolvedValue({ columns: ["date", "visits__count"], rows: [["2026-06-24", 12]], row_count: 1 })
    const graph = artifact()
    const { container, rerender } = render(<ArtifactGraphRenderer artifact={graph} workspaceId="workspace-1" dataRevision="old" />)
    await waitFor(() => expect(screen.getAllByText("12").length).toBeGreaterThan(0))
    const start = screen.getByLabelText("Start date")
    const end = screen.getByLabelText("End date")
    fireEvent.change(start, { target: { value: "2026-06-01" } })
    fireEvent.change(end, { target: { value: "2026-06-30" } })
    await waitFor(() => expect(mockedPost).toHaveBeenLastCalledWith(
      "/api/workspaces/workspace-1/semantic-query/",
      expect.objectContaining({ filters: [expect.objectContaining({ values: ["2026-06-01", "2026-06-30"] })] }),
    ))
    const originalRenderer = container.firstElementChild
    mockedPost.mockClear()
    mockedPost.mockResolvedValue({ columns: ["date", "visits__count"], rows: [["2026-06-24", 27]], row_count: 1 })
    rerender(<ArtifactGraphRenderer artifact={graph} workspaceId="workspace-1" dataRevision="new" />)
    await waitFor(() => expect(screen.getAllByText("27").length).toBeGreaterThan(0))
    expect(start).toHaveValue("2026-06-01")
    expect(end).toHaveValue("2026-06-30")
    expect(screen.getByRole("combobox", { name: "Date range" })).toHaveDisplayValue("Custom dates")
    expect(screen.getByLabelText("Start date")).toBe(start)
    expect(container.firstElementChild).toBe(originalRenderer)
    expect(mockedPost).toHaveBeenCalledTimes(1)
    expect(mockedPost).toHaveBeenLastCalledWith(
      "/api/workspaces/workspace-1/semantic-query/",
      expect.objectContaining({ filters: [expect.objectContaining({ values: ["2026-06-01", "2026-06-30"] })] }),
    )
    rerender(<ArtifactGraphRenderer artifact={graph} workspaceId="workspace-1" dataRevision="new" />)
    expect(mockedPost).toHaveBeenCalledTimes(1)
  })

  it.each(["Start date", "End date"])("labels a manually edited %s as Custom dates", async (label) => {
    mockedPost.mockResolvedValue({ columns: [], rows: [], row_count: 0 })
    render(<ArtifactGraphRenderer artifact={artifact()} workspaceId="workspace-1" />)
    const preset = screen.getByRole("combobox", { name: "Date range" })
    expect(preset).toHaveDisplayValue("Last 7 days")

    fireEvent.change(screen.getByLabelText(label), { target: { value: "2026-06-15" } })

    expect(preset).toHaveDisplayValue("Custom dates")
    expect(screen.getByRole("option", { name: "Custom dates" })).toBeDisabled()
    fireEvent.change(preset, { target: { value: "last_30_days" } })
    expect(preset).toHaveDisplayValue("Last 30 days")
    await waitFor(() => expect(mockedPost).toHaveBeenCalled())
  })

  describe("date filter drafts", () => {
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ["Date"] })
      vi.setSystemTime(new Date("2026-06-30T12:00:00"))
    })

    afterEach(() => vi.useRealTimers())

    function result(count: number) {
      return { columns: ["date", "visits__count"], rows: [["2026-06-24", count]], row_count: 1 }
    }

    function expectRange(start: string, end: string) {
      expect(mockedPost).toHaveBeenLastCalledWith(
        "/api/workspaces/workspace-1/semantic-query/",
        expect.objectContaining({ filters: [expect.objectContaining({ values: [start, end] })] }),
      )
    }

    async function renderRange() {
      mockedPost.mockResolvedValue(result(12))
      const graph = artifact()
      const view = render(<ArtifactGraphRenderer artifact={graph} workspaceId="workspace-1" dataRevision="old" />)
      await waitFor(() => expect(screen.getAllByText("12").length).toBeGreaterThan(0))
      expect(mockedPost).toHaveBeenCalledTimes(1)
      mockedPost.mockClear()
      return {
        ...view,
        graph,
        start: screen.getByLabelText<HTMLInputElement>("Start date"),
        end: screen.getByLabelText<HTMLInputElement>("End date"),
        preset: screen.getByRole("combobox", { name: "Date range" }),
      }
    }

    it("uses readable mobile date controls while preserving their desktop size", async () => {
      const { start, end, preset } = await renderRange()
      for (const control of [start, end, preset]) {
        expect(control).toHaveClass("text-base", "sm:text-sm")
      }
    })

    it.each(["start", "end"] as const)("keeps the last valid range through clearing %s and a recovery refresh", async (field) => {
      const view = await renderRange()
      const input = view[field]
      const appliedStart = view.start.value
      const appliedEnd = view.end.value
      fireEvent.change(input, { target: { value: "" } })

      expect(input).toHaveValue("")
      expect(input).toHaveAttribute("aria-invalid", "true")
      expect(input).toHaveAccessibleDescription(new RegExp(`complete, valid ${field} date.*Still showing`))
      expect(view.preset).toHaveDisplayValue("Custom dates")
      expect(mockedPost).not.toHaveBeenCalled()
      expect(screen.getAllByText("12").length).toBeGreaterThan(0)
      expect(screen.queryByText(/Waiting on .*failed/)).not.toBeInTheDocument()

      mockedPost.mockResolvedValue(result(27))
      view.rerender(<ArtifactGraphRenderer artifact={view.graph} workspaceId="workspace-1" dataRevision="repaired" />)
      await waitFor(() => expect(screen.getAllByText("27").length).toBeGreaterThan(0))
      expect(mockedPost).toHaveBeenCalledTimes(1)
      expectRange(appliedStart, appliedEnd)
      expect(input).toHaveValue("")
      expect(screen.getByLabelText(field === "start" ? "Start date" : "End date")).toBe(input)

      mockedPost.mockClear()
      const repaired = field === "start" ? "2026-06-01" : "2026-06-29"
      fireEvent.change(input, { target: { value: repaired } })
      await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1))
      expectRange(field === "start" ? repaired : appliedStart, field === "end" ? repaired : appliedEnd)
      expect(input).not.toHaveAttribute("aria-invalid")
      expect(screen.getByRole("status")).toHaveTextContent("Dates apply automatically")
      fireEvent.change(input, { target: { value: repaired } })
      expect(mockedPost).toHaveBeenCalledTimes(1)
    })

    it("holds native badInput drafts even while the other endpoint changes", async () => {
      const { start, end } = await renderRange()
      // Browsers serialize a partially entered date as an empty value while
      // retaining the partial segments internally. jsdom needs explicit validity.
      Object.defineProperty(end, "validity", { configurable: true, value: { valid: false, badInput: true } })
      fireEvent.change(end, { target: { value: "" } })
      fireEvent.change(start, { target: { value: "2026-06-01" } })
      expect(mockedPost).not.toHaveBeenCalled()
      expect(end).toHaveAttribute("aria-invalid", "true")
      expect(start).not.toHaveAttribute("aria-invalid")
      expect(screen.getByRole("status")).toHaveTextContent("complete, valid end date")

      // Native invalidity must also win over a seemingly well-formed value.
      fireEvent.change(end, { target: { value: "2026-06-29" } })
      expect(mockedPost).not.toHaveBeenCalled()
      Object.defineProperty(end, "validity", { configurable: true, value: { valid: true, badInput: false } })
      fireEvent.change(end, { target: { value: "2026-06-28" } })
      await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1))
      expectRange("2026-06-01", "2026-06-28")
      expect(end).not.toHaveAttribute("aria-invalid")
    })

    it.each(["2026-02-30", "2026-06-", "0000-06-29"])("never queries a malformed or impossible date: %s", async (date) => {
      const { end } = await renderRange()
      fireEvent.change(end, { target: { value: date } })
      expect(mockedPost).not.toHaveBeenCalled()
      expect(end).toHaveAttribute("aria-invalid", "true")
      expect(screen.getByRole("status")).toHaveTextContent("complete, valid end date")
    })

    it("holds a reversed range until both dates are ordered, then publishes once", async () => {
      const { start, end } = await renderRange()
      fireEvent.change(start, { target: { value: "2026-07-01" } })
      expect(mockedPost).not.toHaveBeenCalled()
      expect(start).toHaveAttribute("aria-invalid", "true")
      expect(end).toHaveAttribute("aria-invalid", "true")
      expect(screen.getByRole("status")).toHaveTextContent("end date on or after the start date")

      fireEvent.change(end, { target: { value: "2026-07-01" } })
      await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1))
      expectRange("2026-07-01", "2026-07-01")
      expect(start).not.toHaveAttribute("aria-invalid")
      expect(end).not.toHaveAttribute("aria-invalid")
    })

    it("presets discard invalid drafts and restoring an unchanged range adds no query", async () => {
      const { start, end, preset } = await renderRange()
      const appliedEnd = end.value
      fireEvent.change(end, { target: { value: "" } })
      fireEvent.change(end, { target: { value: appliedEnd } })
      expect(mockedPost).not.toHaveBeenCalled()
      expect(preset).toHaveDisplayValue("Last 7 days")

      fireEvent.change(start, { target: { value: "" } })
      fireEvent.change(end, { target: { value: "" } })
      expect(screen.getByRole("status")).toHaveTextContent("complete start and end dates")
      fireEvent.change(preset, { target: { value: "last_30_days" } })
      await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1))
      expectRange("2026-06-01", "2026-06-30")
      expect(start).toHaveValue("2026-06-01")
      expect(end).toHaveValue("2026-06-30")
      expect(preset).toHaveDisplayValue("Last 30 days")
      expect(start).not.toHaveAttribute("aria-invalid")
      expect(end).not.toHaveAttribute("aria-invalid")
      fireEvent.change(preset, { target: { value: "last_30_days" } })
      expect(mockedPost).toHaveBeenCalledTimes(1)
    })

    it("keeps newer valid results when an earlier valid date query finishes late", async () => {
      const { start, end } = await renderRange()
      let finishEarlier!: (value: ReturnType<typeof result>) => void
      mockedPost.mockImplementationOnce(() => new Promise((resolve) => { finishEarlier = resolve }))
      mockedPost.mockResolvedValueOnce(result(27))
      fireEvent.change(start, { target: { value: "2026-06-01" } })
      fireEvent.change(end, { target: { value: "2026-06-29" } })
      await waitFor(() => expect(screen.getAllByText("27").length).toBeGreaterThan(0))
      expect(mockedPost).toHaveBeenCalledTimes(2)
      expectRange("2026-06-01", "2026-06-29")
      await act(async () => finishEarlier(result(999)))
      expect(screen.queryByText("999")).not.toBeInTheDocument()
      expect(screen.getAllByText("27").length).toBeGreaterThan(0)
      expect(mockedPost).toHaveBeenCalledTimes(2)
    })
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

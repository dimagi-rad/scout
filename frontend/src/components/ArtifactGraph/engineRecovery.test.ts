import { describe, expect, it, vi } from "vitest"

import { buildStoryRegistry } from "./blocks"
import { StoryEngine } from "./engine"
import type { Row } from "./types"

describe("StoryEngine recovery refresh", () => {
  it("clears an unchanged block's structural error when its missing dependency is added", async () => {
    const runQuery = vi.fn().mockResolvedValue([{ visits_count: 4 }])
    const engine = new StoryEngine(buildStoryRegistry(), { runQuery })
    const table = { id: "table", type: "table", inputs: { data: { $ref: "q.visits" } } }
    engine.loadDoc({ blocks: [table] })
    expect(engine.getDiagnostics()).toEqual(expect.arrayContaining([
      expect.objectContaining({ blockId: "table", message: expect.stringContaining("does not exist") }),
    ]))
    engine.loadDoc({ blocks: [
      table,
      { id: "q", type: "semantic_query", config: { queries: { visits: { measures: ["visits.count"] } } } },
    ] })
    await vi.waitFor(() => expect(engine.getOutput("q.visits").status).toBe("ready"))
    expect(engine.getDiagnostics()).toEqual([])
    await vi.waitFor(() => expect(engine.getOutput("table.data").status).toBe("ready"))
    expect(engine.getOutput("table.data").value).toEqual([{ visits_count: 4 }])
    engine.destroy()
  })

  it("contains invalid source presets and recovers when the block is fixed", async () => {
    const runQuery = vi.fn().mockResolvedValue([{ visits_count: 1 }])
    const engine = new StoryEngine(buildStoryRegistry(), { runQuery })
    const doc = { blocks: [
      { id: "range", type: "date_filter", config: { default: "last_60_days" } },
      { id: "q", type: "semantic_query", inputs: { date_range: { $ref: "range.value" } }, config: {
        queries: { visits: { measures: ["visits.count"], time_dimension: "visits.date" } },
      } },
    ] }
    expect(() => engine.loadDoc(doc)).not.toThrow()
    expect(engine.getOutput("range.value").status).toBe("error")
    expect(engine.getOutput("q.visits").status).toBe("blocked")
    expect(engine.getDiagnostics()).toEqual(expect.arrayContaining([
      expect.objectContaining({ blockId: "range", message: expect.stringContaining("Unsupported date preset") }),
    ]))
    expect(runQuery).not.toHaveBeenCalled()
    const diagnostics = engine.getDiagnostics()
    engine.loadDoc(doc)
    expect(engine.getOutput("range.value").status).toBe("error")
    expect(engine.getDiagnostics()).toEqual(diagnostics)
    doc.blocks[0].config.default = "last_7_days"
    engine.loadDoc(doc)
    await vi.waitFor(() => expect(engine.getOutput("q.visits").status).toBe("ready"))
    expect(runQuery).toHaveBeenCalledTimes(1)
    expect(engine.getDiagnostics()).toEqual([])
    engine.destroy()
  })

  it("preserves selected current and comparison outputs while refreshing both queries", async () => {
    const runQuery = vi.fn().mockResolvedValue([{ visits_count: 1 }])
    const engine = new StoryEngine(buildStoryRegistry(), { runQuery })
    engine.loadDoc({ blocks: [
      { id: "period", type: "period_selector", config: { default_range: "last_7_days" } },
      { id: "q", type: "semantic_query", inputs: { compare: { $ref: "period.pair" } }, config: {
        compare: true, queries: { visits: { measures: ["visits.count"], time_dimension: "visits.date" } },
      } },
    ] })
    await vi.waitFor(() => expect(engine.getOutput("q.visits").status).toBe("ready"))
    const current = { start: "2026-06-01", end: "2026-06-30", preset: "custom" }
    const previous = { start: "2025-06-01", end: "2025-06-30", preset: "previous_year" }
    const pair = { current, previous, label: "Same period last year" }
    engine.setSourceOutputs("period", { current, previous, pair })
    await vi.waitFor(() => expect(engine.getOutput("q.visits").status).toBe("ready"))
    const selected = engine.getOutput("period.pair")
    runQuery.mockClear()
    runQuery.mockResolvedValue([{ visits_count: 7 }])
    engine.refreshData()
    await vi.waitFor(() => expect(engine.getOutput("q.visits").value).toEqual([{ visits_count: 7 }]))
    expect(engine.getOutput("period.pair")).toBe(selected)
    expect(engine.getOutput("period.current").value).toBe(current)
    expect(engine.getOutput("period.previous").value).toBe(previous)
    expect(runQuery).toHaveBeenCalledTimes(2)
    expect(runQuery).toHaveBeenCalledWith(expect.objectContaining({ date_range: current }), expect.anything())
    expect(runQuery).toHaveBeenCalledWith(expect.objectContaining({ date_range: previous }), expect.anything())
    engine.destroy()
  })

  it("aborts stale in-flight evaluations so they cannot overwrite repaired rows", async () => {
    let resolveOld!: (rows: Row[]) => void
    const runQuery = vi.fn()
      .mockReturnValueOnce(new Promise<Row[]>(resolve => { resolveOld = resolve }))
      .mockResolvedValue([{ visits_count: 8 }])
    const engine = new StoryEngine(buildStoryRegistry(), { runQuery })
    engine.loadDoc({ blocks: [
      { id: "q", type: "semantic_query", config: { queries: { visits: { measures: ["visits.count"] } } } },
    ] })
    const oldSignal = runQuery.mock.calls[0][1].signal as AbortSignal
    engine.refreshData()
    expect(oldSignal.aborted).toBe(true)
    await vi.waitFor(() => expect(engine.getOutput("q.visits").value).toEqual([{ visits_count: 8 }]))
    resolveOld([{ visits_count: 1 }])
    await Promise.resolve()
    await Promise.resolve()
    expect(engine.getOutput("q.visits").value).toEqual([{ visits_count: 8 }])
    engine.destroy()
  })
})

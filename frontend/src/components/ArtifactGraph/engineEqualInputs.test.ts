import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { buildStoryRegistry } from "./blocks"
import { StoryEngine } from "./engine"
import type { BlockSpec, Row, StoryDoc, StoryRuntimeContext } from "./types"

const ROWS: Row[] = [
  { day: "2026-09-11", visits_count: 1 },
  { day: "2026-09-12", visits_count: 2 },
  { day: "2026-09-13", visits_count: 3 },
  { day: "2026-09-14", visits_count: 4 },
]

const doc: StoryDoc = { blocks: [
  { id: "range", type: "date_filter", config: { default: "last_30_days" } },
  { id: "q", type: "semantic_query", inputs: { date_range: { $ref: "range.value" } }, config: {
    queries: { visits: { measures: ["visits.count"], time_dimension: "visits.date" } },
  } },
  { id: "chart", type: "graph", inputs: { data: { $ref: "q.visits" } } },
  { id: "table", type: "table", inputs: { data: { $ref: "q.visits" } } },
] }

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function setRange(engine: StoryEngine, start: string) {
  engine.setSourceOutputs("range", {
    value: { start, end: "2026-09-15", preset: "custom" },
  })
}

describe("StoryEngine equal-input recovery", () => {
  const engines: StoryEngine[] = []

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date("2026-09-15T12:00:00Z"))
  })

  afterEach(() => {
    for (const engine of engines.splice(0)) engine.destroy()
    vi.useRealTimers()
  })

  function createEngine(
    runQuery: StoryRuntimeContext["runQuery"],
    registry = buildStoryRegistry(),
    story = doc,
  ) {
    const engine = new StoryEngine(registry, { runQuery })
    engines.push(engine)
    engine.loadDoc(story)
    return engine
  }

  it("restores real graph and table outputs after a date query returns identical rows", async () => {
    const initial = deferred<Row[]>()
    const changed = deferred<Row[]>()
    const runQuery = vi.fn()
      .mockReturnValueOnce(initial.promise)
      .mockReturnValueOnce(changed.promise)
    const engine = createEngine(runQuery)
    initial.resolve(ROWS)
    await vi.runAllTimersAsync()
    for (const ref of ["chart.data", "table.data"]) {
      expect(engine.getOutput(ref)).toMatchObject({ status: "ready", value: ROWS })
    }

    setRange(engine, "2026-07-17")
    expect(runQuery).toHaveBeenCalledTimes(2)
    for (const ref of ["q.visits", "chart.data", "table.data"]) {
      expect(engine.getOutput(ref)).toMatchObject({ status: "pending", value: ROWS })
    }

    changed.resolve(ROWS.map(row => ({ ...row })))
    await vi.runAllTimersAsync()
    expect(runQuery).toHaveBeenCalledTimes(2)
    for (const ref of ["q.visits", "chart.data", "table.data"]) {
      expect(engine.getOutput(ref)).toMatchObject({ status: "ready", value: ROWS })
    }
  })

  it("clears downstream blocked errors when the next successful query returns the same rows", async () => {
    const runQuery = vi.fn().mockResolvedValue(ROWS)
    const engine = createEngine(runQuery)
    await vi.runAllTimersAsync()

    runQuery.mockRejectedValueOnce(new Error("Temporary query failure"))
    setRange(engine, "2026-07-17")
    await vi.runAllTimersAsync()
    expect(engine.getOutput("q.visits").status).toBe("error")
    for (const ref of ["chart.data", "table.data"]) {
      expect(engine.getOutput(ref)).toMatchObject({ status: "blocked", value: ROWS })
      expect(engine.getOutput(ref).error).toContain("Temporary query failure")
    }

    setRange(engine, "2026-07-18")
    await vi.runAllTimersAsync()
    for (const ref of ["q.visits", "chart.data", "table.data"]) {
      expect(engine.getOutput(ref)).toMatchObject({ status: "ready", value: ROWS })
      expect(engine.getOutput(ref).error).toBeUndefined()
    }
    expect(runQuery).toHaveBeenCalledTimes(3)
  })

  it.each([false, true])("restores all computed ports (one port already ready: %s)", async (partialReady) => {
    const split: BlockSpec = {
      type: "split",
      displayName: "Split rows",
      kind: "compute",
      ports: () => ({
        inputs: [{ name: "data", type: "rows", required: true }],
        outputs: [{ name: "first", type: "rows" }, { name: "second", type: "rows" }],
      }),
      evaluate: vi.fn(async ({ inputs }) => ({ first: inputs.data, second: inputs.data })),
    }
    const registry = buildStoryRegistry()
    registry.set(split.type, split)
    const multiPortDoc: StoryDoc = { blocks: [
      ...doc.blocks.slice(0, 2),
      { id: "split", type: "split", inputs: { data: { $ref: "q.visits" } } },
      { id: "chart", type: "graph", inputs: { data: { $ref: "split.first" } } },
      { id: "table", type: "table", inputs: { data: { $ref: "split.second" } } },
    ] }
    const runQuery = vi.fn().mockResolvedValue(ROWS)
    const engine = createEngine(runQuery, registry, multiPortDoc)
    await vi.runAllTimersAsync()
    const firstChanged = vi.fn()
    const secondChanged = vi.fn()
    engine.subscribe("split.first", firstChanged)
    engine.subscribe("split.second", secondChanged)
    const changed = deferred<Row[]>()
    runQuery.mockReturnValueOnce(changed.promise)
    setRange(engine, "2026-07-17")
    expect(engine.getOutput("split.first").status).toBe("pending")
    expect(engine.getOutput("split.second").status).toBe("pending")

    if (partialReady) {
      // The public API can republish individual ports; one ready port must not
      // certify its pending sibling as a reusable result.
      engine.setSourceOutputs("split", { first: ROWS })
      expect(engine.getOutput("split.first").status).toBe("ready")
      expect(engine.getOutput("split.second").status).toBe("pending")
    }
    changed.resolve(ROWS)
    await vi.runAllTimersAsync()
    for (const ref of ["split.first", "split.second", "chart.data", "table.data"]) {
      expect(engine.getOutput(ref)).toMatchObject({ status: "ready", value: ROWS })
    }
    expect(split.evaluate).toHaveBeenCalledTimes(2)
    expect(firstChanged).toHaveBeenCalledTimes(partialReady ? 4 : 2)
    expect(secondChanged).toHaveBeenCalledTimes(2)
  })

  it.each(["before", "after"])("ignores a cancelled query completing %s the latest equal-row query", async (order) => {
    const runQuery = vi.fn().mockResolvedValue(ROWS)
    const engine = createEngine(runQuery)
    await vi.runAllTimersAsync()
    const stale = deferred<Row[]>()
    const latest = deferred<Row[]>()
    runQuery.mockReturnValueOnce(stale.promise).mockReturnValueOnce(latest.promise)

    setRange(engine, "2026-07-17")
    const staleSignal = runQuery.mock.calls[1][1].signal as AbortSignal
    setRange(engine, "2026-07-18")
    expect(staleSignal.aborted).toBe(true)
    expect(runQuery).toHaveBeenCalledTimes(3)

    if (order === "before") {
      stale.resolve([{ visits_count: 999 }])
      await vi.runAllTimersAsync()
      for (const ref of ["q.visits", "chart.data", "table.data"]) {
        expect(engine.getOutput(ref)).toMatchObject({ status: "pending", value: ROWS })
      }
    }
    latest.resolve(ROWS)
    await vi.runAllTimersAsync()
    const refs = ["q.visits", "chart.data", "table.data"]
    const outputs = refs.map(ref => engine.getOutput(ref))
    for (const output of outputs) expect(output).toMatchObject({ status: "ready", value: ROWS })

    if (order === "after") {
      stale.resolve([{ visits_count: 999 }])
      await vi.runAllTimersAsync()
      refs.forEach((ref, index) => expect(engine.getOutput(ref)).toBe(outputs[index]))
    }
  })

  it("restarts cancelled downstream work with the same inputs and ignores its late error", async () => {
    const registry = buildStoryRegistry()
    const graph = registry.get("graph")!
    const stale = deferred<Record<string, unknown>>()
    const latest = deferred<Record<string, unknown>>()
    const evaluate = vi.fn(graph.evaluate!)
      .mockImplementationOnce(graph.evaluate!)
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(latest.promise)
    registry.set("graph", { ...graph, evaluate })
    const runQuery = vi.fn().mockResolvedValue(ROWS)
    const engine = createEngine(runQuery, registry)
    await vi.runAllTimersAsync()
    const changedRows = [{ visits_count: 8 }]
    runQuery.mockResolvedValue(changedRows)
    setRange(engine, "2026-07-17")
    await vi.runAllTimersAsync()
    expect(evaluate).toHaveBeenCalledTimes(2)
    const staleSignal = evaluate.mock.calls[1][0].signal

    setRange(engine, "2026-07-18")
    expect(staleSignal.aborted).toBe(true)
    await vi.runAllTimersAsync()
    // lastSnapshot now matches the cancelled run, while lastOk still refers
    // to the older successful rows. That must not suppress this evaluation.
    expect(evaluate).toHaveBeenCalledTimes(3)
    expect(engine.getOutput("chart.data")).toMatchObject({ status: "pending", value: ROWS })
    latest.resolve({ data: changedRows })
    await vi.runAllTimersAsync()
    const output = engine.getOutput("chart.data")
    expect(output).toMatchObject({ status: "ready", value: changedRows })

    stale.reject(new Error("Superseded transform failed"))
    await vi.runAllTimersAsync()
    expect(engine.getOutput("chart.data")).toBe(output)
    expect(engine.getOutput("table.data")).toMatchObject({ status: "ready", value: changedRows })
  })

  it("debounces repeated equal-input recovery without firing cancelled timers", async () => {
    const registry = buildStoryRegistry()
    const graph = registry.get("graph")!
    const evaluate = vi.fn(graph.evaluate!)
    registry.set("graph", { ...graph, evaluate, debounceMs: 50 })
    const runQuery = vi.fn().mockResolvedValue(ROWS)
    const engine = createEngine(runQuery, registry)
    await vi.runAllTimersAsync()
    expect(evaluate).toHaveBeenCalledTimes(1)

    setRange(engine, "2026-07-17")
    await vi.advanceTimersByTimeAsync(25)
    setRange(engine, "2026-07-18")
    await vi.advanceTimersByTimeAsync(49)
    expect(evaluate).toHaveBeenCalledTimes(1)
    expect(engine.getOutput("chart.data").status).toBe("pending")
    await vi.advanceTimersByTimeAsync(1)
    expect(evaluate).toHaveBeenCalledTimes(2)
    expect(engine.getOutput("chart.data")).toMatchObject({ status: "ready", value: ROWS })
    expect(runQuery).toHaveBeenCalledTimes(3)
    expect(vi.getTimerCount()).toBe(0)
  })

  it("still reuses equal snapshots when every output is already ready", async () => {
    const registry = buildStoryRegistry()
    const graph = registry.get("graph")!
    const evaluate = vi.fn(graph.evaluate!)
    registry.set("graph", { ...graph, evaluate })
    const runQuery = vi.fn().mockResolvedValue(ROWS)
    const engine = createEngine(runQuery, registry)
    await vi.runAllTimersAsync()
    const refs = ["q.visits", "chart.data", "table.data"]
    const outputs = refs.map(ref => engine.getOutput(ref))

    engine.loadDoc(doc)
    await vi.runAllTimersAsync()
    expect(runQuery).toHaveBeenCalledTimes(1)
    expect(evaluate).toHaveBeenCalledTimes(1)
    refs.forEach((ref, index) => expect(engine.getOutput(ref)).toBe(outputs[index]))
  })

  it("restarts carried pending outputs when reloading an unchanged document", async () => {
    const runQuery = vi.fn().mockResolvedValue(ROWS)
    const engine = createEngine(runQuery)
    await vi.runAllTimersAsync()
    const stale = deferred<Row[]>()
    runQuery.mockReturnValueOnce(stale.promise)
    setRange(engine, "2026-07-17")
    const staleSignal = runQuery.mock.calls[1][1].signal as AbortSignal

    engine.loadDoc(doc)
    expect(staleSignal.aborted).toBe(true)
    await vi.runAllTimersAsync()
    expect(runQuery).toHaveBeenCalledTimes(3)
    for (const ref of ["q.visits", "chart.data", "table.data"]) {
      expect(engine.getOutput(ref)).toMatchObject({ status: "ready", value: ROWS })
    }
    const output = engine.getOutput("q.visits")
    stale.resolve([{ visits_count: 999 }])
    await vi.runAllTimersAsync()
    expect(engine.getOutput("q.visits")).toBe(output)
  })
})

import { describe, expect, it } from "vitest"
import { pivotSeriesRows } from "./seriesPivot"

const options = { xKey: "week", yKey: "count", seriesBy: "segment", fillMissing: true }

describe("long-format series pivot", () => {
  it("preserves x and dimension order, sums duplicate pairs and numeric strings without mutating input", () => {
    const rows = [
      { week: "Jun 2", segment: "Top user", count: "2" },
      { week: "May 26", segment: "Everyone else", count: 8 },
      { week: "Jun 2", segment: "Top user", count: 3 },
      { week: "Jun 2", segment: "Everyone else", count: 4 },
    ]
    const before = structuredClone(rows)
    const result = pivotSeriesRows(rows, options)
    expect(result.series.map((s) => s.label)).toEqual(["Top user", "Everyone else"])
    const [a, b] = result.series.map((s) => s.data_key)
    expect(result.rows).toEqual([
      { week: "Jun 2", [a]: 5, [b]: 4 },
      { week: "May 26", [a]: 0, [b]: 8 },
    ])
    expect(rows).toEqual(before)
  })

  it("leaves absent line combinations as gaps", () => {
    const result = pivotSeriesRows([
      { week: "a", segment: "A", count: 1 },
      { week: "b", segment: "B", count: 2 },
    ], { ...options, fillMissing: false })
    expect(result.rows[0][result.series[1].data_key]).toBeNull()
    expect(result.rows[1][result.series[0].data_key]).toBeNull()
  })

  it("caps at five using top four totals and Other, preserving totals and x order", () => {
    const result = pivotSeriesRows(Array.from({ length: 8 }, (_, i) => ({
      week: "a", segment: `S${i}`, count: i + 1,
    })), options)
    expect(result.series.map((s) => s.label)).toEqual(["S7", "S6", "S5", "S4", "Other"])
    expect(result.series.map((s) => result.rows[0][s.data_key])).toEqual([8, 7, 6, 5, 10])
  })

  it("uses safe generated keys even for dimension values resembling paths or prototype properties", () => {
    const labels = ["__proto__", "constructor", "a.b", "week", "__series_0"]
    const result = pivotSeriesRows(labels.map((segment) => ({ week: "a", segment, count: 2 })), options)
    expect(result.series.map((s) => s.label)).toEqual(labels)
    expect(result.series.every((s) => !s.data_key.includes("."))).toBe(true)
    expect(result.series.map((s) => result.rows[0][s.data_key])).toEqual([2, 2, 2, 2, 2])
    expect(result.rows[0].week).toBe("a")
  })

  it("does not collide generated keys with the x field", () => {
    const result = pivotSeriesRows([{ __series_0: "a", segment: "X", count: 3 }], { ...options, xKey: "__series_0" })
    expect(result.series[0].data_key).not.toBe("__series_0")
    expect(result.rows[0].__series_0).toBe("a")
  })

  it("distinguishes null values and literal fallback labels, including aggregated Other", () => {
    const values = [null, "(Missing)", "Other", "A", "B", "C"]
    const result = pivotSeriesRows(values.map((segment, i) => ({ week: "a", segment, count: 10 - i })), options)
    const labels = result.series.map((s) => s.label)
    expect(new Set(labels).size).toBe(labels.length)
    expect(labels).toContain("Other")
    expect(labels).toContain("Other (remaining)")
    expect(result.series.reduce((total, s) => total + Number(result.rows[0][s.data_key]), 0)).toBe(45)
  })

  it("handles empty results", () => {
    expect(pivotSeriesRows([], options)).toEqual({ rows: [], series: [] })
  })

  it.each(["", "invalid", Infinity, true])("rejects non-numeric measures (%s) instead of silently changing totals", (count) => {
    expect(() => pivotSeriesRows([{ week: "a", segment: "A", count }], options)).toThrow("numeric")
  })

  it("rejects missing fields in any row", () => {
    expect(() => pivotSeriesRows([{ week: "a", segment: "A", count: 1 }, { week: "b", count: 2 }], options)).toThrow("segment")
  })

  it.each([true, false])("preserves null measures as missing combinations (fillMissing=%s)", (fillMissing) => {
    const result = pivotSeriesRows([
      { week: "a", segment: "A", count: null },
      { week: "b", segment: "B", count: 2 },
      { week: "b", segment: "B", count: null },
    ], { ...options, fillMissing })
    const [a, b] = result.series.map((s) => s.data_key)
    expect(result.rows).toEqual([
      { week: "a", [a]: fillMissing ? 0 : null, [b]: fillMissing ? 0 : null },
      { week: "b", [a]: fillMissing ? 0 : null, [b]: 2 },
    ])
  })

  it("keeps Other as a gap when its contributing measures are all null", () => {
    const rows = Array.from({ length: 6 }, (_, i) => ({ week: "a", segment: `S${i}`, count: 6 - i }))
    const result = pivotSeriesRows([...rows, { week: "b", segment: "S5", count: null }], { ...options, fillMissing: false })
    expect(result.rows[1][result.series[4].data_key]).toBeNull()
  })
})

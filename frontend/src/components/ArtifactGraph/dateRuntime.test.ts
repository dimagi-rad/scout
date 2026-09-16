import { describe, expect, it } from "vitest"

import { comparisonPeriod, previousPeriod, resolvePresetRange } from "./runtime"

describe("deterministic calendar dates", () => {
  it("does not convert a calendar date to the previous UTC day in positive-offset browsers", () => {
    // Run this suite with TZ=Asia/Singapore as well as America/New_York.
    const range = resolvePresetRange("today", new Date("2026-09-16T12:00:00Z"))
    expect(range).toEqual({ start: "2026-09-16", end: "2026-09-16", preset: "today" })
  })
  it("includes exactly N calendar days and rejects unknown presets", () => {
    expect(resolvePresetRange("last_90_days", new Date("2026-09-16T12:00:00Z"))).toEqual({ start: "2026-06-19", end: "2026-09-16", preset: "last_90_days" })
    expect(() => resolvePresetRange("last_31_days")).toThrow("Unsupported date preset")
  })
  it("keeps adjacent comparison windows across DST and clamps leap days", () => {
    expect(previousPeriod({ start: "2026-03-03", end: "2026-03-09" })).toEqual({ start: "2026-02-24", end: "2026-03-02", preset: "previous_period" })
    expect(comparisonPeriod({ start: "2024-02-29", end: "2024-03-02" }, "previous_year")).toEqual({ start: "2023-02-28", end: "2023-03-02", preset: "previous_year" })
  })
})

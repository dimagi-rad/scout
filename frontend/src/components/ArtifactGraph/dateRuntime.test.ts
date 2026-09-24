import { describe, expect, it } from "vitest"

import { comparisonPeriod, previousPeriod, resolvePresetRange } from "./runtime"
import type { DateContext } from "./types"

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
  it.each(["constructor", "toString", "valueOf", "__proto__"])("rejects inherited server preset %s", (preset) => {
    const context: DateContext = {
      as_of: "2026-09-24T12:00:00Z", timezone: "UTC", today: "2026-09-24", presets: {},
    }
    expect(() => resolvePresetRange(preset, context)).toThrow("Unsupported date preset")
  })
  it("calculates missing comparison metadata from the selected bounds", () => {
    const range = { start: "2026-03-03", end: "2026-03-09", preset: "last_7_days" }
    const context: DateContext = {
      as_of: "2026-03-09T12:00:00Z", timezone: "UTC", today: "2026-03-09",
      presets: { last_7_days: { ...range, comparisons: {} } },
    }
    expect(comparisonPeriod(range, "previous_period", context)).toEqual({
      start: "2026-02-24", end: "2026-03-02", preset: "previous_period",
    })
  })
})

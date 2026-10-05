import { describe, expect, it } from "vitest"
import { formatValue } from "./format"

describe("formatValue", () => {
  it("preserves numeric strings without metadata, even with explicit numeric formats", () => {
    expect(formatValue("33504507934753956376")).toBe("33504507934753956376")
    expect(formatValue("001234", "number")).toBe("001234")
    expect(formatValue("1234.5")).toBe("1234.5")
  })
  it("preserves string and dimension values regardless of numeric format", () => {
    expect(formatValue("001234", "currency", { field_type: "dimension", data_type: "integer" })).toBe("001234")
    expect(formatValue(1234, "number", { field_type: "dimension" })).toBe("1234")
    expect(formatValue("1234", "number", { data_type: "text" })).toBe("1234")
  })
  it("formats numeric measures but preserves unsafe numeric strings", () => {
    expect(formatValue("1234", "number", { field_type: "measure", data_type: "integer" })).toBe("1,234")
    expect(formatValue("33504507934753956376", "number", { field_type: "measure", data_type: "integer" })).toBe("33504507934753956376")
    expect(formatValue("1.0000000000000000001", "number_19", { field_type: "measure", data_type: "decimal" })).toBe("1.0000000000000000001")
    expect(formatValue("1.0000000000000000001e-2", "number_19", { field_type: "measure" })).toBe("1.0000000000000000001e-2")
    expect(formatValue("1e-1000", "number", { field_type: "measure" })).toBe("1e-1000")
    expect(formatValue(1234.5)).toBe("1,234.5")
    expect(formatValue(0.25, "percent")).toBe("25%")
    expect(formatValue(null)).toBe("-")
  })
  it.each(["day", "week", "month", "quarter", "year"])("formats %s buckets as local calendar dates", (granularity) => {
    expect(formatValue("2025-05-26T00:00:00.000", undefined, { field_type: "time_dimension", granularity }))
      .toBe(new Date(2025, 4, 26).toLocaleDateString())
  })
  it("includes time for finer buckets and unbucketed timestamps", () => {
    const value = "2025-05-26T14:30:00.000"
    for (const granularity of ["hour", "minute", "second", undefined]) {
      expect(formatValue(value, undefined, { field_type: "time_dimension", granularity }))
        .toBe(new Date(value).toLocaleString())
    }
  })
  it("keeps invalid date strings intact", () => {
    expect(formatValue("not-a-date", "date")).toBe("not-a-date")
    expect(formatValue("2025-02-31", "date")).toBe("2025-02-31")
    expect(formatValue("2025-99-99", undefined, { field_type: "time_dimension", granularity: "day" })).toBe("2025-99-99")
  })
})

it("formats uppercase warehouse DATE types as calendar dates", () => {
  expect(formatValue("2025-05-26T00:00:00.000", undefined, { field_type: "dimension", data_type: "DATE" }))
    .toBe(new Date(2025, 4, 26).toLocaleDateString())
})

it.each(["hour", "minute", "second"])("keeps time in %s buckets even when the declared source type is DATE", (granularity) => {
  const value = "2025-05-26T14:30:00.000"
  expect(formatValue(value, undefined, { field_type: "time_dimension", data_type: "DATE", granularity }))
    .toBe(new Date(value).toLocaleString())
})

it("treats date-only datetime values as local calendar dates", () => {
  expect(formatValue("2026-06-24", "datetime"))
    .toBe(new Date(2026, 5, 24).toLocaleString())
})

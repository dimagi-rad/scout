import { afterEach, describe, expect, it, vi } from "vitest"
import { comparisonPeriod, resolvePresetRange } from "./runtime"

afterEach(() => {
  vi.unstubAllEnvs()
})

// Artifact reporting dates use the server context, not the viewer's timezone.
// Offline Date inputs use UTC consistently, including across DST boundaries.
describe.each(["Pacific/Kiritimati", "Asia/Kolkata", "UTC", "America/Los_Angeles", "Pacific/Pago_Pago"])(
  "date ranges in %s",
  (tz) => {
    it("resolves offline presets to the UTC reporting day in every viewer timezone", () => {
      vi.stubEnv("TZ", tz)
      const justAfterMidnight = new Date("2025-09-22T00:30:00Z")

      expect(resolvePresetRange("last_7_days", justAfterMidnight)).toEqual({
        start: "2025-09-16",
        end: "2025-09-22",
        preset: "last_7_days",
      })
      expect(resolvePresetRange("month_to_date", justAfterMidnight)).toEqual({
        start: "2025-09-01",
        end: "2025-09-22",
        preset: "month_to_date",
      })
    })

    it("shifts comparison ranges by whole reporting days across DST changes", () => {
      vi.stubEnv("TZ", tz)
      const range = { start: "2025-10-27", end: "2025-11-09", preset: "custom" }

      expect(comparisonPeriod(range, "previous_period")).toEqual({
        start: "2025-10-13",
        end: "2025-10-26",
        preset: "previous_period",
      })
      expect(comparisonPeriod(range, "previous_year")).toEqual({
        start: "2024-10-27",
        end: "2024-11-09",
        preset: "previous_year",
      })
    })
  },
)

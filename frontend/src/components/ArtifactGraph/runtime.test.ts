import { afterEach, describe, expect, it, vi } from "vitest"
import { comparisonPeriod, resolvePresetRange } from "./runtime"

afterEach(() => {
  vi.unstubAllEnvs()
})

// CI runs in UTC, where a UTC-based date formatter looks correct. Node honours a
// runtime TZ change, so pin zones on both sides of UTC to keep an off-by-one-day
// regression from passing there.
describe.each(["Pacific/Kiritimati", "Asia/Kolkata", "UTC", "America/Los_Angeles", "Pacific/Pago_Pago"])(
  "date ranges in %s",
  (tz) => {
    it("resolves presets to the local calendar day", () => {
      vi.stubEnv("TZ", tz)
      const justAfterMidnight = new Date(2025, 8, 22, 0, 30)

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

    it("shifts comparison ranges by whole local days across DST changes", () => {
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

import { describe, expect, it } from "vitest"
import { formatTimeEstimate } from "./timeEstimate"

describe("formatTimeEstimate", () => {
  it("omits missing and invalid estimates", () => {
    expect(formatTimeEstimate(null)).toBeNull()
    expect(formatTimeEstimate({ usual_seconds: 0, elapsed_seconds: null })).toBeNull()
    expect(formatTimeEstimate({ usual_seconds: NaN, elapsed_seconds: null })).toBeNull()
  })
  it("shows a usual duration before a load starts", () => {
    expect(formatTimeEstimate({ usual_seconds: 240, elapsed_seconds: null }))
      .toBe("Usually takes about 4 min")
  })
  it("rounds short durations without seconds", () => {
    expect(formatTimeEstimate({ usual_seconds: 59, elapsed_seconds: null }))
      .toBe("Usually takes under a minute")
    expect(formatTimeEstimate({ usual_seconds: 90, elapsed_seconds: null }))
      .toBe("Usually takes about 2 min")
  })
  it("shows approximate remaining time", () => {
    expect(formatTimeEstimate({ usual_seconds: 240, elapsed_seconds: 120 }))
      .toBe("About 2 min left · usually takes about 4 min")
    expect(formatTimeEstimate({ usual_seconds: 240, elapsed_seconds: 200 }))
      .toBe("Under a minute left · usually takes about 4 min")
  })
  it("never shows a negative or zero countdown", () => {
    for (const elapsed of [240, 241, 10000]) {
      expect(formatTimeEstimate({ usual_seconds: 240, elapsed_seconds: elapsed }))
        .toBe("Taking longer than usual")
    }
  })
  it("does not turn invalid elapsed time into a countdown", () => {
    for (const elapsed of [NaN, Infinity, -1]) {
      expect(formatTimeEstimate({ usual_seconds: 240, elapsed_seconds: elapsed }))
        .toBe("Usually takes about 4 min")
    }
  })
})

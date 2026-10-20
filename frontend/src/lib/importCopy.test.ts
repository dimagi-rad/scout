import { describe, expect, it } from "vitest"

import { importingLabel, importingSourceLabel } from "./importCopy"

describe("importingLabel", () => {
  it.each([
    ["commcare_connect", "Importing from Connect…"],
    ["ocs", "Importing from OCS…"],
    ["commcare", "Importing from CommCare…"],
    ["commcare_eu", "Importing from CommCare…"],
  ])("names the %s provider", (provider, label) => {
    expect(importingLabel(provider)).toBe(label)
  })

  it("names one provider shared by every source", () => {
    expect(importingLabel(["ocs", "ocs"])).toBe("Importing from OCS…")
  })

  it.each([
    ["mixed providers", ["commcare", "ocs"]],
    ["a source with no provider", ["ocs", null]],
    ["no provider", null],
    ["an unknown provider", "dhis2"],
  ] as const)("says Importing data… for %s", (_, providers) => {
    expect(importingLabel(providers)).toBe("Importing data…")
  })
})

describe("importingSourceLabel", () => {
  it("names the source and its provider", () => {
    expect(importingSourceLabel("visits", "commcare_connect")).toBe("Importing visits from Connect…")
  })

  it("falls back to whichever half is known", () => {
    expect(importingSourceLabel("visits", null)).toBe("Importing visits…")
    expect(importingSourceLabel("visits", "dhis2")).toBe("Importing visits…")
    expect(importingSourceLabel(null, "ocs")).toBe("Importing from OCS…")
    expect(importingSourceLabel(null, null)).toBe("Importing data…")
  })
})

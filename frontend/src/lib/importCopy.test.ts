import { describe, expect, it } from "vitest"

import { importingLabel, importingSourceLabel } from "./importCopy"

describe("importingLabel", () => {
  it.each([
    ["commcare_connect", "Importing from CommCare Connect…"],
    ["ocs", "Importing from Open Chat Studio…"],
    ["commcare", "Importing from CommCare HQ…"],
    ["commcare_eu", "Importing from CommCare HQ…"],
  ])("names the %s provider", (provider, label) => {
    expect(importingLabel(provider)).toBe(label)
  })

  it("names one provider shared by every source", () => {
    expect(importingLabel(["ocs", "ocs"])).toBe("Importing from Open Chat Studio…")
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
    expect(importingSourceLabel("visits", "commcare_connect")).toBe("Importing visits from CommCare Connect…")
  })

  it("falls back to whichever half is known", () => {
    expect(importingSourceLabel("visits", null)).toBe("Importing visits…")
    expect(importingSourceLabel("visits", "dhis2")).toBe("Importing visits…")
    expect(importingSourceLabel(null, "ocs")).toBe("Importing from Open Chat Studio…")
    expect(importingSourceLabel(null, null)).toBe("Importing data…")
  })
})

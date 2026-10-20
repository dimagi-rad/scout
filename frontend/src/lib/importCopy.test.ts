import { describe, expect, it } from "vitest"

import { importingLabel } from "./importCopy"

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

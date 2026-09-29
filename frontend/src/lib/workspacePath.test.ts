import { describe, expect, it } from "vitest"

import { workspacePath } from "./workspacePath"

describe("workspacePath", () => {
  it("slugifies a multi-source label", () => {
    const path = workspacePath({ id: "abc", display_name: "Malaria Study · 3 sources" })
    expect(path).toBe("/workspaces/malaria-study-3-sources/abc")
  })

  it("keeps same-named workspaces on distinct URLs via the id", () => {
    const a = workspacePath({ id: "id-a", display_name: "Malaria Study · 3 sources" })
    const b = workspacePath({ id: "id-b", display_name: "Malaria Study · 3 sources" })
    expect(a).not.toBe(b)
  })
})

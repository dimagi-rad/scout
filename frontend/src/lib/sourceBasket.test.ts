import { describe, expect, it } from "vitest"
import { combinedWorkspaceName } from "./sourceBasket"

describe("combinedWorkspaceName", () => {
  it("uses the leading words the sources share", () => {
    expect(combinedWorkspaceName(["CHC Nutrition Kenya", "CHC Nutrition Uganda"])).toBe(
      "CHC Nutrition (2 sources)",
    )
  })

  it("joins two unrelated names", () => {
    expect(combinedWorkspaceName(["Alpha", "Beta"])).toBe("Alpha + Beta")
  })

  it("summarises several unrelated names", () => {
    expect(combinedWorkspaceName(["Alpha", "Beta", "Gamma"])).toBe("Alpha + 2 more")
  })

  it("drops trailing punctuation from the shared words", () => {
    expect(combinedWorkspaceName(["KMC - Kenya", "KMC - Malawi"])).toBe("KMC (2 sources)")
  })
})

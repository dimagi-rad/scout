import { describe, expect, it } from "vitest"
import { parseShareToken, shareApiUrl } from "@/lib/shareToken"

// Issue #248, finding 04#8(c): PublicThreadPage / PublicRecipeRunPage derived
// the share token from the UNSTRIPPED pathname with a regex anchored at
// ^/shared/. Under the labs base path the pathname is /scout/shared/<token>, so
// the regex never matched, the token came back undefined, and the page hung on
// an endless skeleton. The base must be stripped before matching.

describe("parseShareToken", () => {
  it("parses a thread token at the root mount", () => {
    expect(parseShareToken("/shared/threads/abc-123", "threads", "")).toBe("abc-123")
  })

  it("parses a run token at the root mount", () => {
    expect(parseShareToken("/shared/runs/run-tok", "runs", "")).toBe("run-tok")
  })

  it("parses a thread token under a /scout base path", () => {
    expect(parseShareToken("/scout/shared/threads/abc-123", "threads", "/scout")).toBe(
      "abc-123",
    )
  })

  it("parses a run token under a /scout base path with a trailing slash", () => {
    expect(parseShareToken("/scout/shared/runs/run-tok/", "runs", "/scout")).toBe(
      "run-tok",
    )
  })

  it("ignores trailing path segments", () => {
    expect(parseShareToken("/shared/threads/tok/extra/stuff", "threads", "")).toBe("tok")
  })

  it("returns undefined for a non-matching path", () => {
    expect(parseShareToken("/scout/dashboard", "threads", "/scout")).toBeUndefined()
  })

  it("does not treat the wrong share kind as a match", () => {
    expect(parseShareToken("/shared/runs/tok", "threads", "")).toBeUndefined()
  })

  it("parses a recipe token under a /scout base path", () => {
    expect(parseShareToken("/scout/shared/recipes/r-tok", "recipes", "/scout")).toBe(
      "r-tok",
    )
  })
})

describe("shareApiUrl (BASE_PATH === '')", () => {
  it("maps each share kind to its API path", () => {
    expect(shareApiUrl("threads", "t")).toBe("/api/chat/threads/shared/t/")
    expect(shareApiUrl("runs", "r")).toBe("/api/recipes/runs/shared/r/")
    expect(shareApiUrl("recipes", "rec")).toBe("/api/recipes/shared/rec/")
  })
})

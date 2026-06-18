import { describe, expect, it } from "vitest"
import { joinBase, stripBase, stripBasePath, withBasePath } from "@/config"

// Issue #248, finding 04#8: a number of URLs were built root-relative
// (e.g. fetch("/health/")) which bypasses VITE_BASE_PATH=/scout on the labs
// deployment (nginx only proxies /scout/...). These helpers centralize
// base-path joining/stripping so URLs respect the configured mount point.

describe("joinBase (pure)", () => {
  it("prefixes a leading-slash path with a non-empty base", () => {
    expect(joinBase("/scout", "/health/")).toBe("/scout/health/")
    expect(joinBase("/scout", "/api/chat/threads/shared/tok/")).toBe(
      "/scout/api/chat/threads/shared/tok/",
    )
  })

  it("normalizes a path missing its leading slash", () => {
    expect(joinBase("/scout", "health/")).toBe("/scout/health/")
    expect(joinBase("", "health/")).toBe("/health/")
  })

  it("returns the path unchanged when base is empty", () => {
    expect(joinBase("", "/health/")).toBe("/health/")
  })
})

describe("stripBase (pure)", () => {
  it("removes a non-empty base prefix", () => {
    expect(stripBase("/scout", "/scout/shared/threads/tok-123")).toBe(
      "/shared/threads/tok-123",
    )
  })

  it("maps the bare base to root", () => {
    expect(stripBase("/scout", "/scout")).toBe("/")
  })

  it("does not strip a base that is only a substring prefix", () => {
    // "/scoutland" must NOT be treated as base "/scout" + "/land"
    expect(stripBase("/scout", "/scoutland/x")).toBe("/scoutland/x")
  })

  it("returns the pathname unchanged when base is empty", () => {
    expect(stripBase("", "/shared/threads/tok-123")).toBe("/shared/threads/tok-123")
  })
})

// The exported helpers close over BASE_PATH, which is "" in the test env
// (VITE_BASE_PATH unset), so they behave as identity-ish here.
describe("withBasePath / stripBasePath (env-bound, BASE_PATH === '')", () => {
  it("withBasePath normalizes but does not prefix", () => {
    expect(withBasePath("/health/")).toBe("/health/")
    expect(withBasePath("health/")).toBe("/health/")
  })

  it("stripBasePath returns the pathname unchanged", () => {
    expect(stripBasePath("/shared/threads/tok-123")).toBe("/shared/threads/tok-123")
  })
})

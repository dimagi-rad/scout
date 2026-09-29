import * as Sentry from "@sentry/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  dropConsoleBreadcrumb,
  reportRenderError,
  resetReportedRenderErrorsForTests,
} from "./reportRenderError"

const scope = vi.hoisted(() => ({ setTag: vi.fn() }))

vi.mock("@sentry/react", () => ({
  captureException: vi.fn(),
  withScope: vi.fn((callback: (s: typeof scope) => void) => callback(scope)),
}))

describe("reportRenderError", () => {
  beforeEach(() => resetReportedRenderErrorsForTests())
  afterEach(() => vi.clearAllMocks())

  it("keeps crashes distinct when their messages redact to the same text", () => {
    for (const property of ["visits", "name"]) {
      reportRenderError({
        source: "sandbox",
        name: "TypeError",
        message: `Cannot read properties of undefined (reading '${property}')`,
        artifactId: "artifact-one",
      })
    }

    const messages = vi.mocked(Sentry.captureException).mock.calls
      .map(([error]) => (error as Error).message)
    expect(messages).toEqual([
      'Cannot read properties of undefined (reading "…")',
      'Cannot read properties of undefined (reading "…")',
    ])
  })

  it("keeps a noisy sandbox from using up the boundary's budget", () => {
    for (let i = 0; i < 25; i++) {
      reportRenderError({ source: "sandbox", name: "Error", message: `noise ${i}` })
    }
    expect(Sentry.captureException).toHaveBeenCalledTimes(20)

    reportRenderError({ source: "boundary", name: "Error", message: "app crash" })
    expect(Sentry.captureException).toHaveBeenCalledTimes(21)
  })

  it("drops console breadcrumbs, which hold raw error text", () => {
    expect(dropConsoleBreadcrumb({ category: "console", message: 'bad "Alice"' })).toBeNull()
    const navigation = { category: "navigation", message: "/artifacts/artifact-one" }
    expect(dropConsoleBreadcrumb(navigation)).toBe(navigation)
  })
})

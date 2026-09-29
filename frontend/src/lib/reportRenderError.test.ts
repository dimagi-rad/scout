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

  it("reports the same error again when it recurs at a different stage", () => {
    const crash = { source: "sandbox" as const, name: "TypeError", message: "boom", artifactId: "a" }
    reportRenderError({ ...crash, stage: "React Render Error" })
    reportRenderError({ ...crash, stage: "Uncaught Error" })

    expect(Sentry.captureException).toHaveBeenCalledTimes(2)
    expect(scope.setTag).toHaveBeenCalledWith("render_error_stage", "React Render Error")
    expect(scope.setTag).toHaveBeenCalledWith("render_error_stage", "Uncaught Error")
  })

  it("cannot be made to report fake stack frames through the name or message", () => {
    reportRenderError({
      source: "sandbox",
      name: "X\n    at evil (a:1:1)",
      message: "first\n    at forged (b:2:2)",
      stack: "    at App (sandbox:1:1)",
    })

    const reported = vi.mocked(Sentry.captureException).mock.calls[0][0] as Error
    expect(reported.name).toBe("Error")
    expect(reported.message).toBe("first at forged (b:2:2)")
    expect(reported.stack).toBe("Error: render error\n    at App (sandbox:1:1)")
  })

  it("keeps a message that looks like a frame out of the stack", () => {
    reportRenderError({
      source: "sandbox",
      name: "UnhandledRejection",
      message: "x@https://evil.example/a.js:2:2",
      stack: "    at App (sandbox:1:1)",
    })

    const reported = vi.mocked(Sentry.captureException).mock.calls[0][0] as Error
    expect(reported.message).toBe("x@https://evil.example/a.js:2:2")
    expect(reported.stack).toBe("Error: render error\n    at App (sandbox:1:1)")
  })

  it("keeps the top frame of a minified React error, whose first line Sentry skips", async () => {
    reportRenderError({
      source: "boundary",
      name: "Error",
      message: "Minified React error #418; visit https://react.dev/errors/418 for the full message",
      stack: "Error: Minified React error #418\n    at App (app.js:1:1)\n    at render (app.js:2:2)",
    })

    const reported = vi.mocked(Sentry.captureException).mock.calls[0][0] as Error
    const { defaultStackParser } = await vi.importActual<typeof Sentry>("@sentry/react")
    const frames = defaultStackParser(reported.stack ?? "", 1).map((frame) => frame.function)
    expect(frames).toEqual(["render", "App"])
  })

  it("names a thrown non-Error, which has no name, Error", () => {
    const thrown = "boom" as unknown as Error
    reportRenderError({ source: "boundary", name: thrown.name, message: thrown.message })

    const reported = vi.mocked(Sentry.captureException).mock.calls[0][0] as Error
    expect(reported.name).toBe("Error")
  })

  it("redacts a quoted value that is broken across lines", () => {
    reportRenderError({
      source: "sandbox",
      name: "Error",
      message: 'invalid input syntax for type integer: "Alice\nBob"\u2028next',
    })

    const reported = vi.mocked(Sentry.captureException).mock.calls[0][0] as Error
    expect(reported.message).toBe('invalid input syntax for type integer: "…" next')
  })

  it("drops console breadcrumbs, which hold raw error text", () => {
    expect(dropConsoleBreadcrumb({ category: "console", message: 'bad "Alice"' })).toBeNull()
    const navigation = { category: "navigation", message: "/artifacts/artifact-one" }
    expect(dropConsoleBreadcrumb(navigation)).toBe(navigation)
  })
})

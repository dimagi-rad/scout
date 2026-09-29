import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import * as Sentry from "@sentry/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { resetReportedRenderErrorsForTests } from "@/lib/reportRenderError"
import { ErrorBoundary } from "./ErrorBoundary"

const scope = vi.hoisted(() => ({ setTag: vi.fn() }))

vi.mock("@sentry/react", () => ({
  captureException: vi.fn(),
  withScope: vi.fn((callback: (s: typeof scope) => void) => callback(scope)),
}))

function Broken(): never {
  throw new RangeError('Invalid time value for "2026-13-45"')
}

describe("ErrorBoundary Sentry reporting", () => {
  beforeEach(() => {
    resetReportedRenderErrorsForTests()
    vi.spyOn(console, "error").mockImplementation(() => {})
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.clearAllMocks()
  })

  it("reports a caught render error once with the artifact context", async () => {
    render(
      <ErrorBoundary artifactId="artifact-one" artifactVersion={2}>
        <Broken />
      </ErrorBoundary>,
    )

    expect(Sentry.captureException).toHaveBeenCalledTimes(1)
    const reported = vi.mocked(Sentry.captureException).mock.calls[0][0] as Error
    expect(reported.name).toBe("RangeError")
    expect(reported.message).toBe('Invalid time value for "…"')
    expect(reported.stack).not.toContain("2026-13-45")
    expect(scope.setTag).toHaveBeenCalledWith("artifact_id", "artifact-one")
    expect(scope.setTag).toHaveBeenCalledWith("artifact_version", "2")
    expect(scope.setTag).toHaveBeenCalledWith("render_error_source", "boundary")

    await userEvent.click(screen.getByRole("button", { name: /try again/i }))
    expect(screen.getByText("Something went wrong")).toBeInTheDocument()
    expect(Sentry.captureException).toHaveBeenCalledTimes(1)
  })

  it.each([
    [null, "Non-Error exception (null)"],
    [{ message: "Alice", name: "Bob" }, "Non-Error exception (object)"],
    ["boom", "boom"],
  ])("reports a thrown %j by its type, not its fields", (value, message) => {
    function ThrowsValue(): never {
      throw value
    }
    render(
      <ErrorBoundary>
        <ThrowsValue />
      </ErrorBoundary>,
    )

    const reported = vi.mocked(Sentry.captureException).mock.calls[0][0] as Error
    expect(reported.name).toBe("Error")
    expect(reported.message).toBe(message)
    expect(screen.getByText("Something went wrong")).toBeInTheDocument()
    expect(screen.queryByText("Alice")).not.toBeInTheDocument()
  })

  it("renders its fallback when the thrown object's message is not text", () => {
    function ThrowsObject(): never {
      throw { message: { rows: [1] } }
    }
    render(
      <ErrorBoundary>
        <ThrowsObject />
      </ErrorBoundary>,
    )

    expect(screen.getByText("Something went wrong")).toBeInTheDocument()
    expect(screen.getByText("Non-Error exception (object)")).toBeInTheDocument()
  })

  it("clears the caught error when resetKey changes", () => {
    let broken = true
    function MaybeBroken() {
      if (broken) throw new Error("stale data")
      return <p>Recovered chart</p>
    }
    const { rerender } = render(
      <ErrorBoundary resetKey="revision-one">
        <MaybeBroken />
      </ErrorBoundary>,
    )
    expect(screen.getByText("Something went wrong")).toBeInTheDocument()

    broken = false
    rerender(
      <ErrorBoundary resetKey="revision-two">
        <MaybeBroken />
      </ErrorBoundary>,
    )
    expect(screen.getByText("Recovered chart")).toBeInTheDocument()
  })
})

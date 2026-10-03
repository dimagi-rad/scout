import * as Sentry from "@sentry/react"
import { render, screen } from "@testing-library/react"
import { createMemoryRouter, Outlet, RouterProvider } from "react-router-dom"
import { afterEach, describe, expect, it, vi } from "vitest"

import { reportBoundaryError, resetReportedRenderErrorsForTests } from "./reportRenderError"

const scope = vi.hoisted(() => ({ setTag: vi.fn() }))

vi.mock("@sentry/react", () => ({
  captureException: vi.fn(),
  withScope: vi.fn((callback: (s: typeof scope) => void) => callback(scope)),
}))

function BrokenSidebar(): never {
  throw new TypeError("sidebar exploded")
}

describe("router onError reporting", () => {
  afterEach(() => {
    resetReportedRenderErrorsForTests()
    vi.clearAllMocks()
  })

  it("reports a crash in a layout outside any app boundary", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {})
    const router = createMemoryRouter([
      {
        path: "/",
        element: (
          <>
            <BrokenSidebar />
            <Outlet />
          </>
        ),
      },
    ])

    render(<RouterProvider router={router} onError={(error) => reportBoundaryError(error)} />)

    await screen.findByText(/Unexpected Application Error/)
    const reported = vi.mocked(Sentry.captureException).mock.calls[0][0] as Error
    expect(reported.name).toBe("TypeError")
    expect(reported.message).toBe("sidebar exploded")
    expect(scope.setTag).toHaveBeenCalledWith("render_error_source", "boundary")
  })
})

import { describe, expect, it, vi } from "vitest"
import { act, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { createBusyTracker } from "@/api/busy"
import { BusyNotice } from "./BusyNotice"

describe("BusyNotice", () => {
  it("renders nothing while Scout is not busy", () => {
    render(<BusyNotice tracker={createBusyTracker()} />)
    expect(screen.queryByTestId("busy-notice")).toBeNull()
  })

  it("says it is retrying while a request backs off", () => {
    const tracker = createBusyTracker()
    render(<BusyNotice tracker={tracker} />)

    act(() => tracker.startRetry(Symbol("request")))

    expect(screen.getByTestId("busy-notice-message")).toHaveTextContent("Scout is busy — retrying…")
    expect(screen.queryByTestId("busy-notice-retry")).toBeNull()
  })

  it("offers a manual Retry once automatic retries are spent", async () => {
    const tracker = createBusyTracker()
    const onRetry = vi.fn()
    render(<BusyNotice tracker={tracker} onRetry={onRetry} />)

    act(() => tracker.gaveUp())
    expect(screen.getByTestId("busy-notice-message")).toHaveTextContent("Scout is still busy")

    await userEvent.click(screen.getByTestId("busy-notice-retry"))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it("lets the user dismiss the notice", async () => {
    const tracker = createBusyTracker()
    render(<BusyNotice tracker={tracker} onRetry={vi.fn()} />)

    act(() => tracker.gaveUp())
    await userEvent.click(screen.getByTestId("busy-notice-dismiss"))

    expect(screen.queryByTestId("busy-notice")).toBeNull()
  })

  it("stays reachable above modal overlays and the offline bar", () => {
    const tracker = createBusyTracker()
    render(<BusyNotice tracker={tracker} />)
    act(() => tracker.gaveUp())

    expect(screen.getByTestId("busy-notice-region").className).toContain("z-[60]")
    expect(screen.getByTestId("busy-notice").className).toContain("pointer-events-auto")
  })
})

import { describe, expect, it } from "vitest"
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
    render(<BusyNotice tracker={tracker} />)

    let decision: Promise<string> = Promise.resolve("")
    act(() => {
      decision = tracker.awaitManualRetry(Symbol("request"))
    })
    expect(screen.getByTestId("busy-notice-message")).toHaveTextContent("Scout is still busy")

    await userEvent.click(screen.getByTestId("busy-notice-retry"))

    await expect(decision).resolves.toBe("retry")
    expect(screen.queryByTestId("busy-notice")).toBeNull()
  })

  it("lets the user dismiss the notice", async () => {
    const tracker = createBusyTracker()
    render(<BusyNotice tracker={tracker} />)

    let decision: Promise<string> = Promise.resolve("")
    act(() => {
      decision = tracker.awaitManualRetry(Symbol("request"))
    })
    await userEvent.click(screen.getByTestId("busy-notice-dismiss"))

    await expect(decision).resolves.toBe("dismiss")
    expect(screen.queryByTestId("busy-notice")).toBeNull()
  })
})

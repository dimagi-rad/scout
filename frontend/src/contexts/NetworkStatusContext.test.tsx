import { afterEach, describe, expect, it, vi } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"

import { OfflineBanner } from "@/components/OfflineBanner/OfflineBanner"
import { NetworkStatusProvider } from "./NetworkStatusContext"

afterEach(() => vi.unstubAllGlobals())

function healthReturns(status: number, body: unknown) {
  const fetchMock = vi.fn().mockImplementation(
    async () => new Response(JSON.stringify(body), { status }),
  )
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

function renderBanner() {
  return render(
    <NetworkStatusProvider>
      <OfflineBanner />
    </NetworkStatusProvider>,
  )
}

describe("server reachability", () => {
  it("keeps the red reconnect bar away when the server is only busy", async () => {
    const fetchMock = healthReturns(503, {
      status: "busy",
      checks: { database: "busy", queue: "busy" },
    })
    renderBanner()

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.queryByTestId("offline-banner")).toBeNull()
  })

  it("still shows the bar when the server is really unhealthy", async () => {
    healthReturns(503, { status: "unhealthy", checks: { database: "error", queue: "ok" } })
    renderBanner()

    expect(await screen.findByTestId("offline-banner")).toHaveTextContent("Server unreachable")
  })
})

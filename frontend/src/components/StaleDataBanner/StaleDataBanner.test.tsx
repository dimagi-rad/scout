import { act, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api/client"
import { jobsApi } from "@/api/jobs"
import {
  workspaceApi,
  type SourceFreshnessDetail,
  type WorkspaceFreshness,
  type WorkspaceListItem,
} from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { READ_ONLY_REFRESH_NOTE, StaleDataBanner } from "./StaleDataBanner"
import { dismissStaleBanner } from "./staleData"
import { freshness, freshSource } from "./testFixtures"

const WS = "ws-1"

function source(hoursAgo: number | null, extra: Partial<SourceFreshnessDetail> = {}) {
  return freshSource("Alpha", hoursAgo, extra)
}

function mockDetail(sources: SourceFreshnessDetail[], extra: Partial<WorkspaceFreshness> = {}) {
  return vi.spyOn(workspaceApi, "getFreshness").mockResolvedValue(freshness(sources, extra))
}

function asRole(role: WorkspaceListItem["role"]) {
  useAppStore.setState({ domains: [{ id: WS, role } as WorkspaceListItem] })
}

function renderBanner(props: Partial<Parameters<typeof StaleDataBanner>[0]> = {}) {
  return render(
    <MemoryRouter>
      <StaleDataBanner workspaceId={WS} {...props} />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  sessionStorage.clear()
  asRole("read_write")
})

afterEach(() => {
  vi.restoreAllMocks()
  useAppStore.setState({ domains: [] })
})

describe("StaleDataBanner", () => {
  it("offers a refresh when the data is past the threshold", async () => {
    mockDetail([source(72)])
    renderBanner()

    expect(await screen.findByTestId("stale-data-banner-message")).toHaveTextContent(
      "This data was last refreshed 3 days ago. Refresh it now?",
    )
    expect(screen.getByTestId("stale-data-banner-refresh")).toBeEnabled()
  })

  it("shows hours under 48 hours", async () => {
    mockDetail([source(30)])
    renderBanner()

    expect(await screen.findByTestId("stale-data-banner-message")).toHaveTextContent(
      "last refreshed 30 hours ago",
    )
  })

  it("stays hidden for fresh data", async () => {
    const spy = mockDetail([source(2)])
    renderBanner()

    await vi.waitFor(() => expect(spy).toHaveBeenCalled())
    expect(screen.queryByTestId("stale-data-banner")).toBeNull()
  })

  it("stays hidden while a load is in progress", async () => {
    const spy = mockDetail([source(72)], { in_progress: true })
    renderBanner()

    await vi.waitFor(() => expect(spy).toHaveBeenCalled())
    expect(screen.queryByTestId("stale-data-banner")).toBeNull()
  })

  it("hides when a load starts and does not refetch until it ends", async () => {
    const spy = mockDetail([source(72)])
    const { rerender } = renderBanner()
    await screen.findByTestId("stale-data-banner")

    rerender(
      <MemoryRouter>
        <StaleDataBanner workspaceId={WS} loading />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId("stale-data-banner")).toBeNull()
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it("stays hidden when nothing is loaded yet", async () => {
    const spy = mockDetail([source(null)])
    renderBanner()

    await vi.waitFor(() => expect(spy).toHaveBeenCalled())
    expect(screen.queryByTestId("stale-data-banner")).toBeNull()
  })

  it("refreshes through the same endpoint as Retry", async () => {
    mockDetail([source(72)])
    const retry = vi
      .spyOn(jobsApi, "retryMaterialization")
      .mockResolvedValue({ status: "started" })
    const onRefreshStarted = vi.fn()
    renderBanner({ onRefreshStarted })

    fireEvent.click(await screen.findByTestId("stale-data-banner-refresh"))

    await vi.waitFor(() => expect(onRefreshStarted).toHaveBeenCalled())
    expect(retry).toHaveBeenCalledWith(WS, {})
    expect(screen.getByTestId("stale-data-banner-refresh")).toBeDisabled()
  })

  it("shows the server's reason when the refresh is refused", async () => {
    mockDetail([source(72)])
    vi.spyOn(jobsApi, "retryMaterialization").mockRejectedValue(
      new ApiError(403, "Your CommCare sign-in expired.", {
        error: "Your CommCare sign-in expired.",
      }),
    )
    renderBanner()

    fireEvent.click(await screen.findByTestId("stale-data-banner-refresh"))

    expect(await screen.findByTestId("stale-data-banner-error")).toHaveTextContent(
      "Your CommCare sign-in expired.",
    )
  })

  it("shows read-only members the age without a Refresh button", async () => {
    asRole("read")
    mockDetail([source(72)])
    renderBanner()

    expect(await screen.findByTestId("stale-data-banner-message")).toHaveTextContent(
      `last refreshed 3 days ago. ${READ_ONLY_REFRESH_NOTE}`,
    )
    expect(screen.queryByTestId("stale-data-banner-refresh")).toBeNull()
  })

  it("says reconnect instead of refresh when the viewer's sign-in expired", async () => {
    mockDetail([source(72, { reconnect: true })])
    renderBanner()

    const link = await screen.findByTestId("stale-data-banner-reconnect")
    expect(link).toHaveTextContent("Reconnect CommCare HQ")
    expect(link).toHaveAttribute("href", "/settings/connections")
    expect(screen.queryByTestId("stale-data-banner-refresh")).toBeNull()
  })

  it("keeps Refresh beside the reconnect when another stale source can still sync", async () => {
    mockDetail([
      freshSource("Alpha", 100),
      freshSource("Beta", 72, { reconnect: true }),
    ])
    renderBanner()

    expect(await screen.findByTestId("stale-data-banner-reconnect")).toBeInTheDocument()
    expect(screen.getByTestId("stale-data-banner-refresh")).toBeEnabled()
    expect(screen.getByTestId("stale-data-banner-message")).toHaveTextContent(
      "Alpha's data was last refreshed 4 days ago. Refresh it now? Your CommCare HQ sign-in expired",
    )
  })

  it("tells a read-only member who can refresh rather than to reconnect", async () => {
    asRole("read")
    mockDetail([source(72, { reconnect: true })])
    renderBanner()

    expect(await screen.findByTestId("stale-data-banner-message")).toHaveTextContent(
      READ_ONLY_REFRESH_NOTE,
    )
    expect(screen.queryByTestId("stale-data-banner-reconnect")).toBeNull()
  })

  it("remembers a dismiss for the workspace this session", async () => {
    const spy = mockDetail([source(72)])
    const first = renderBanner()

    fireEvent.click(await screen.findByTestId("stale-data-banner-dismiss"))
    expect(screen.queryByTestId("stale-data-banner")).toBeNull()
    first.unmount()

    renderBanner()
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
    expect(screen.queryByTestId("stale-data-banner")).toBeNull()
  })

  it("does not carry a dismiss over to another workspace", async () => {
    dismissStaleBanner("other-ws")
    mockDetail([source(72)])
    renderBanner()

    expect(await screen.findByTestId("stale-data-banner")).toBeInTheDocument()
  })

  it("still dismisses when session storage is unavailable", async () => {
    mockDetail([source(72)])
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied")
    })
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied")
    })
    renderBanner()

    const dismiss = await screen.findByTestId("stale-data-banner-dismiss")
    act(() => dismiss.click())
    expect(screen.queryByTestId("stale-data-banner")).toBeNull()
  })
})

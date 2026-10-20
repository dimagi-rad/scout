import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { workspaceApi, type WorkspaceListItem } from "@/api/workspaces"
import { freshness, freshSource } from "@/components/StaleDataBanner/testFixtures"
import { useAppStore } from "@/store/store"
import { ChatPanel } from "./ChatPanel"

const jobs = vi.hoisted(() => ({ workspaceLoads: [] as unknown[] }))

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: {},
    workspaceLoads: jobs.workspaceLoads,
    recentlyCompletedThreadIds: [],
    recentTerminationsByToolCallId: {},
    pendingByThreadId: {},
    setPendingRequest: vi.fn(),
    hidePendingRequest: vi.fn(),
    forgetPendingRequest: vi.fn(),
    refresh: vi.fn(),
    notifyJobLikelyStarted: vi.fn(),
  }),
}))

const WS = "11111111-1111-1111-1111-111111111111"
const THREAD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const RUNNING_LOAD = {
  tenant_id: "t1", tenant_name: "Alpha", source_index: 1, source_total: 1,
  started_at: "2026-09-30T00:00:00Z", state: "running", progress: null,
}
const loaded = freshness([
  freshSource("Alpha", 0, {
    summary: { total_rows: 42, tables: [{ name: "forms", state: "completed", rows: 42 }] },
  }),
])

beforeEach(() => {
  sessionStorage.clear()
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith("/messages/?include=pending")) return Response.json([])
      if (url.endsWith("/artifacts/")) return Response.json({ results: [] })
      throw new Error(`Unexpected request: ${url}`)
    }),
  )
  useAppStore.setState({
    activeDomainId: WS,
    domains: [{ id: WS, role: "manage", tenants: [] } as unknown as WorkspaceListItem],
    domainsStatus: "loaded",
    threadId: THREAD,
    threads: [],
    threadsStatus: "loaded",
  })
  vi.spyOn(workspaceApi, "getFreshness").mockResolvedValue(loaded)
})

afterEach(() => {
  jobs.workspaceLoads = []
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("ChatPanel load summary", () => {
  it("opens by itself when a load in the workspace finishes", async () => {
    jobs.workspaceLoads = [RUNNING_LOAD]
    const { rerender } = render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByTestId("workspace-load-banner")

    jobs.workspaceLoads = []
    rerender(<MemoryRouter><ChatPanel /></MemoryRouter>)

    const summary = await screen.findByTestId("load-summary")
    expect(summary).toHaveTextContent("Data load finished")
    expect(screen.getByTestId("load-summary-table-Alpha-forms")).toHaveTextContent("42")
  })

  it("stays closed when no load ran", async () => {
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    await vi.waitFor(() => expect(workspaceApi.getFreshness).toHaveBeenCalled())
    expect(screen.queryByTestId("load-summary")).toBeNull()
  })
})

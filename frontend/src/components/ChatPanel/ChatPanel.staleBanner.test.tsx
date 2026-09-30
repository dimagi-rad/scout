import type { UIMessage } from "ai"
import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { workspaceApi, type WorkspaceDetail, type WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { ChatPanel } from "./ChatPanel"

const jobs = vi.hoisted(() => ({ workspaceLoads: [] as unknown[] }))

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: {},
    workspaceLoads: jobs.workspaceLoads,
    recentlyCompletedThreadIds: [],
    recentTerminationsByToolCallId: {},
    notifyJobLikelyStarted: vi.fn(),
  }),
}))

const WS = "11111111-1111-1111-1111-111111111111"
const THREAD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

function mockMessages(messages: UIMessage[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith("/messages/")) return Response.json(messages)
      if (url.endsWith("/artifacts/")) return Response.json({ results: [] })
      throw new Error(`Unexpected request: ${url}`)
    }),
  )
}

beforeEach(() => {
  jobs.workspaceLoads = []
  sessionStorage.clear()
  useAppStore.setState({ activeDomainId: WS })
  useAppStore.setState({
    domains: [{ id: WS, role: "manage", tenants: [] } as unknown as WorkspaceListItem],
    domainsStatus: "loaded",
    threadId: THREAD,
    threads: [],
    threadsStatus: "loaded",
  })
  vi.spyOn(workspaceApi, "getDetail").mockResolvedValue({
    id: WS,
    stale_data_banner_hours: 24,
    sources: [
      {
        tenant_id: "t1",
        tenant_name: "Alpha",
        provider: "commcare",
        provider_label: "CommCare HQ",
        last_synced_at: new Date(Date.now() - 72 * 3600_000).toISOString(),
        serving: true,
      },
    ],
  } as WorkspaceDetail)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("ChatPanel stale-data banner", () => {
  it("shows on a new chat", async () => {
    mockMessages([])
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    expect(await screen.findByTestId("stale-data-banner")).toBeInTheDocument()
  })

  it("shows on an existing chat", async () => {
    mockMessages([{ id: "m1", role: "assistant", parts: [{ type: "text", text: "Earlier answer" }] }])
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    await screen.findByText("Earlier answer")
    expect(await screen.findByTestId("stale-data-banner")).toBeInTheDocument()
  })

  it("hides while a teammate's load is running", async () => {
    jobs.workspaceLoads = [
      { tenant_id: "t1", tenant_name: "Alpha", source_index: 1, source_total: 1, started_at: "2026-09-30T00:00:00Z", state: "running", progress: null },
    ]
    mockMessages([])
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    await screen.findByTestId("workspace-load-banner")
    expect(screen.queryByTestId("stale-data-banner")).toBeNull()
  })
})

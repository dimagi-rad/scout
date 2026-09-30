import type { UIMessage } from "ai"
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
  vi.spyOn(workspaceApi, "getFreshness").mockResolvedValue(
    freshness([freshSource("Alpha", 72)]),
  )
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

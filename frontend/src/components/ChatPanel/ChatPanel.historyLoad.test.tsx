import type { UIMessage } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { WorkspaceListItem } from "@/api/workspaces"
import { ChatPanel } from "./ChatPanel"

vi.mock("./historyLoad", () => ({ HISTORY_LOAD_TIMEOUT_MS: 50 }))

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: {},
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
const HISTORY: UIMessage[] = [{
  id: "saved", role: "assistant", parts: [{ type: "text", text: "Saved answer." }],
}]

function workspace(): WorkspaceListItem {
  return {
    id: WS, name: "Workspace", display_name: "Workspace", is_auto_created: false, role: "manage",
    tenants: [], member_count: 1, schema_status: "available", last_synced_at: null,
    created_at: "2026-01-01",
  }
}

/** History loads answer in order: "hold" until released, "stall" until aborted, or "ok". */
function mockServer(loads: ("hold" | "stall" | "ok")[]) {
  let release!: () => void
  const held = new Promise<void>((resolve) => (release = resolve))
  const messageLoads: string[] = []
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    if (/\/threads\/[^/]+\/messages\//.test(url)) {
      const mode = loads.shift() ?? "ok"
      messageLoads.push(mode)
      if (mode === "hold") await held
      if (mode === "stall") {
        await new Promise((_, reject) => {
          options?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          )
        })
      }
      return Response.json(HISTORY)
    }
    if (/\/threads\/[^/]+\/viewed\/$/.test(url)) return new Response(null, { status: 204 })
    if (/\/threads\/$/.test(url)) return Response.json([])
    return Response.json({}, { status: 404 })
  }))
  return { release, messageLoads }
}

function typeText(text: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } })
}

beforeEach(() => {
  localStorage.clear()
  useAppStore.setState({ domains: [workspace()], domainsStatus: "loaded", activeDomainId: WS })
  useAppStore.setState({ threadId: THREAD, threads: [], threadsStatus: "loaded" })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("a thread's history load", () => {
  it("shows the composer and a loading line while it loads, and sends once loaded", async () => {
    const server = mockServer(["hold"])
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    expect(await screen.findByTestId("chat-history-loading")).toBeInTheDocument()
    await act(async () => typeText("draft"))
    expect(screen.getByRole("textbox")).toHaveValue("draft")
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled()

    await act(async () => server.release())
    await screen.findByText("Saved answer.")
    expect(screen.queryByTestId("chat-history-loading")).toBeNull()
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled()
  })

  it("gives up on a stalled load, offers a retry, and loads on retry", async () => {
    const server = mockServer(["stall", "ok"])
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    const retry = await screen.findByTestId("chat-history-retry")
    expect(screen.queryByTestId("chat-history-loading")).toBeNull()
    // A failed load must not block sending for good.
    await act(async () => typeText("still here"))
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled()

    await act(async () => fireEvent.click(retry))
    await screen.findByText("Saved answer.")
    expect(screen.queryByTestId("chat-history-retry")).toBeNull()
    expect(server.messageLoads).toEqual(["stall", "ok"])
  })

  it("loads once a workspace arrives after the thread was set", async () => {
    mockServer(["ok"])
    useAppStore.setState({ activeDomainId: null })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    expect(screen.getByText("Select a domain to start chatting")).toBeInTheDocument()

    // Selecting a workspace starts a fresh thread; the URL then picks the saved one.
    await act(async () => useAppStore.setState({ activeDomainId: WS }))
    await act(async () => useAppStore.setState({ threadId: THREAD }))
    await screen.findByText("Saved answer.")
    await act(async () => typeText("hello"))
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled(),
    )
  })
})

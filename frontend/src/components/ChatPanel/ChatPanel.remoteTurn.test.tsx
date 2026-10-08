import type { UIMessage } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { WorkspaceListItem } from "@/api/workspaces"
import { ChatPanel } from "./ChatPanel"
import { REMOTE_TURN_MAX_POLL_MS, REMOTE_TURN_POLL_MS } from "./useRemoteTurn"

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
const OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
const QUESTION: UIMessage = {
  id: "q", role: "user", parts: [{ type: "text", text: "Visits by district?" }],
}
const ANSWER: UIMessage = {
  id: "a", role: "assistant", parts: [{ type: "text", text: "Here are the visits." }],
}

function workspace(): WorkspaceListItem {
  return {
    id: WS, name: "Workspace", display_name: "Workspace", is_auto_created: false, role: "manage",
    tenants: [], member_count: 1, schema_status: "available", last_synced_at: null,
    created_at: "2026-01-01",
  }
}

/** A server whose THREAD turn runs (in another tab) until ``finish``. */
function mockServer() {
  const server = {
    running: true,
    detailPolls: [] as string[],
    listFetches: 0,
    chatPosts: 0,
    finish() {
      server.running = false
    },
  }
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === "/api/chat/") {
      server.chatPosts += 1
      return Response.json({}, { status: 500 })
    }
    const messages = url.match(/\/threads\/([^/]+)\/messages\//)
    if (messages) {
      const mine = messages[1] === THREAD
      return Response.json({
        messages: mine ? (server.running ? [QUESTION] : [QUESTION, ANSWER]) : [],
        pending_request: null,
        turn_running: mine && server.running,
      })
    }
    const detail = url.match(/\/threads\/([^/]+)\/$/)
    if (detail) {
      server.detailPolls.push(detail[1])
      return Response.json({ id: detail[1], turn_running: server.running })
    }
    if (/\/threads\/[^/]+\/viewed\/$/.test(url)) return new Response(null, { status: 204 })
    if (/\/workspaces\/[^/]+\/threads\/$/.test(url)) {
      server.listFetches += 1
      return Response.json(useAppStore.getState().threads)
    }
    return Response.json({}, { status: 404 })
  }))
  return server
}

function renderPanel() {
  return render(<MemoryRouter><ChatPanel /></MemoryRouter>)
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout"] })
  localStorage.clear()
  useAppStore.setState({ domains: [workspace()], domainsStatus: "loaded", activeDomainId: WS })
  useAppStore.setState({ threadId: THREAD, threads: [], threadsStatus: "loaded" })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("a thread whose turn runs where this tab can't follow it (#856)", () => {
  it("shows a placeholder and blocks sending while it runs", async () => {
    const server = mockServer()
    renderPanel()

    expect(await screen.findByTestId("chat-remote-turn")).toHaveTextContent(
      "Still working on this…",
    )
    expect(screen.getByText("Visits by district?")).toBeInTheDocument()
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "and by month?" } })
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled()
    await act(async () => {
      fireEvent.submit(screen.getByRole("textbox").closest("form")!)
    })
    expect(server.chatPosts).toBe(0)
    expect(screen.getByRole("textbox")).toHaveValue("and by month?")
  })

  it("polls until the turn ends, then reloads the thread and lets you send", async () => {
    const server = mockServer()
    renderPanel()
    await screen.findByTestId("chat-remote-turn")
    const listFetches = server.listFetches

    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_POLL_MS))
    expect(server.detailPolls).toEqual([THREAD])
    // Still running: the next poll backs off past the base interval.
    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_POLL_MS))
    expect(server.detailPolls).toHaveLength(1)

    server.finish()
    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_POLL_MS))
    expect(server.detailPolls).toHaveLength(2)

    expect(await screen.findByText("Here are the visits.")).toBeInTheDocument()
    expect(screen.queryByTestId("chat-remote-turn")).toBeNull()
    expect(server.listFetches).toBeGreaterThan(listFetches)
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "and by month?" } })
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled()

    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_MAX_POLL_MS * 2))
    expect(server.detailPolls).toHaveLength(2)
  })

  it("recovers once a dead holder's lease lapses, polling no faster than the cap", async () => {
    const server = mockServer()
    renderPanel()
    await screen.findByTestId("chat-remote-turn")

    // The lease of a holder that died lapses after 90s.
    await act(() => vi.advanceTimersByTimeAsync(90_000))
    const polls = server.detailPolls.length
    expect(polls).toBeGreaterThan(3)
    expect(polls).toBeLessThanOrEqual(90_000 / REMOTE_TURN_POLL_MS)
    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_MAX_POLL_MS))
    expect(server.detailPolls.length).toBe(polls + 1)

    server.finish()
    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_MAX_POLL_MS))
    expect(await screen.findByText("Here are the visits.")).toBeInTheDocument()
    expect(screen.queryByTestId("chat-remote-turn")).toBeNull()
  })

  it("stops polling when another thread is shown", async () => {
    const server = mockServer()
    renderPanel()
    await screen.findByTestId("chat-remote-turn")

    await act(async () => useAppStore.setState({ threadId: OTHER }))
    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_MAX_POLL_MS * 2))

    expect(server.detailPolls).toEqual([])
    expect(screen.queryByTestId("chat-remote-turn")).toBeNull()
  })

  it("stops polling on unmount", async () => {
    const server = mockServer()
    const { unmount } = renderPanel()
    await screen.findByTestId("chat-remote-turn")

    unmount()
    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_MAX_POLL_MS * 2))

    expect(server.detailPolls).toEqual([])
  })

  it("shows no placeholder for a thread whose turn is not running", async () => {
    const server = mockServer()
    server.finish()
    renderPanel()

    expect(await screen.findByText("Here are the visits.")).toBeInTheDocument()
    expect(screen.queryByTestId("chat-remote-turn")).toBeNull()
    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_MAX_POLL_MS))
    expect(server.detailPolls).toEqual([])
  })

  it("refetches the list when a send in a listed thread is answered, to show it running", async () => {
    const server = mockServer()
    server.finish()
    useAppStore.setState({
      threads: [{
        id: THREAD, title: "Visits", title_is_custom: false, title_source: "generated",
        created_at: "2026-07-01T12:00:00Z", updated_at: "2026-07-01T12:00:00Z",
        last_viewed_at: null, turn_running: false,
      }],
    })
    renderPanel()
    await screen.findByText("Here are the visits.")
    const listFetches = server.listFetches

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "and by month?" } })
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })))

    await vi.waitFor(() => expect(server.chatPosts).toBe(1))
    await vi.waitFor(() => expect(server.listFetches).toBe(listFetches + 1))
  })
})

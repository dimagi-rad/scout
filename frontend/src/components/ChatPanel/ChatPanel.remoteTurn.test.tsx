import { createUIMessageStream, createUIMessageStreamResponse, type UIMessage } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { WorkspaceListItem } from "@/api/workspaces"
import { ChatPanel } from "./ChatPanel"
import type { ActiveJob } from "@/api/jobs"
import {
  REMOTE_TURN_MAX_FAILURES,
  REMOTE_TURN_MAX_POLL_MS,
  REMOTE_TURN_POLL_MS,
} from "./useRemoteTurn"

const jobs = vi.hoisted(() => ({ byThreadId: {} as Record<string, unknown> }))

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: jobs.byThreadId,
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
    messageLoads: 0,
    listFetches: 0,
    chatPosts: 0,
    /** Detail polls answer with this status instead, while set. */
    detailStatus: null as number | null,
    /** Message loads answer with this status instead, while set. */
    messagesStatus: null as number | null,
    chatReply: "stream" as "stream" | "busy" | "network",
    /** What the running turn has streamed for chats that did not start it. */
    liveRows: [] as { id: number; run: string; text: string; done: boolean }[],
    tailReads: 0,
    /** Holds this tab's own turn open until called. */
    endReply: () => {},
    finish() {
      server.running = false
    },
  }
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === "/api/chat/") {
      server.chatPosts += 1
      if (server.chatReply === "network") throw new TypeError("Failed to fetch")
      if (server.chatReply === "busy") {
        return Response.json({ error: "A response is still being generated." }, { status: 409 })
      }
      const ended = new Promise<void>((resolve) => (server.endReply = resolve))
      return createUIMessageStreamResponse({
        stream: createUIMessageStream({
          execute: async ({ writer }) => {
            writer.write({ type: "start" })
            writer.write({ type: "text-start", id: "t" })
            writer.write({ type: "text-delta", id: "t", delta: "By month: rising." })
            await ended
            writer.write({ type: "text-end", id: "t" })
            writer.write({ type: "finish", finishReason: "stop" })
          },
        }),
      })
    }
    const messages = url.match(/\/threads\/([^/]+)\/messages\//)
    if (messages) {
      server.messageLoads += 1
      if (server.messagesStatus) return Response.json({}, { status: server.messagesStatus })
      const mine = messages[1] === THREAD
      return Response.json({
        messages: mine ? (server.running ? [QUESTION] : [QUESTION, ANSWER]) : [],
        pending_request: null,
        turn_running: mine && server.running,
      })
    }
    const tail = url.match(/\/threads\/([^/]+)\/resume-stream\/\?after=(\d+)/)
    if (tail) {
      server.tailReads += 1
      const after = Number(tail[2])
      const rows = tail[1] === THREAD ? server.liveRows.filter((row) => row.id > after) : []
      return Response.json({ chunks: rows, more: false })
    }
    const detail = url.match(/\/threads\/([^/]+)\/$/)
    if (detail) {
      server.detailPolls.push(detail[1])
      if (server.detailStatus) return Response.json({}, { status: server.detailStatus })
      return Response.json({ id: detail[1], turn_running: server.running })
    }
    if (/\/threads\/[^/]+\/viewed\/$/.test(url)) return new Response(null, { status: 204 })
    if (/\/workspaces\/[^/]+\/threads\/$/.test(url)) {
      server.listFetches += 1
      return Response.json(
        useAppStore.getState().threads.map((thread) => ({
          ...thread, turn_running: thread.id === THREAD ? server.running : false,
        })),
      )
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
  useAppStore.setState({
    threadId: THREAD, threads: [], threadsStatus: "loaded", localTurnThreadIds: new Set(),
  })
  jobs.byThreadId = {}
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

  it("stops waiting after repeated failed polls and lets the reload decide", async () => {
    const server = mockServer()
    renderPanel()
    await screen.findByTestId("chat-remote-turn")
    server.detailStatus = 403
    const loads = server.messageLoads

    // 3 + 4.5 + 6.75 + 10.1 + 15s: the fifth failure ends the wait.
    await act(() => vi.advanceTimersByTimeAsync(39_000))
    expect(server.detailPolls).toHaveLength(REMOTE_TURN_MAX_FAILURES - 1)
    await act(() => vi.advanceTimersByTimeAsync(1_000))
    expect(server.detailPolls).toHaveLength(REMOTE_TURN_MAX_FAILURES)
    await vi.waitFor(() => expect(server.messageLoads).toBe(loads + 1))

    // The reload still reports the turn running, so the wait starts over.
    expect(await screen.findByTestId("chat-remote-turn")).toBeInTheDocument()
    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_POLL_MS))
    expect(server.detailPolls).toHaveLength(REMOTE_TURN_MAX_FAILURES + 1)
    server.detailStatus = null
    server.finish()
    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_MAX_POLL_MS))
    expect(await screen.findByText("Here are the visits.")).toBeInTheDocument()
  })

  it("keeps following a running turn when a reload it asked for fails", async () => {
    const server = mockServer()
    server.liveRows = [{ id: 1, run: "call-1", text: "Let me check.", done: false }]
    renderPanel()
    await screen.findByTestId("resume-stream")
    server.messagesStatus = 503

    // The first call ends at its tool; the reload for it fails.
    server.liveRows.push({ id: 2, run: "call-1", text: "", done: true })
    await act(() => vi.advanceTimersByTimeAsync(2_000))
    expect(await screen.findByTestId("chat-history-retry")).toBeInTheDocument()
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "and by month?" } })
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled()

    // Still following: the turn's end reloads it.
    server.messagesStatus = null
    server.finish()
    await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_MAX_POLL_MS))
    expect(await screen.findByText("Here are the visits.")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled()
  })

  it("leaves a background resume's own progress to show, without the placeholder", async () => {
    mockServer()
    const job: ActiveJob = {
      thread_job_id: "job-1", thread_id: THREAD, tool_call_id: "toolu_1",
      job_type: "materialization", state: "running", progress: null, source_index: null,
      source_total: null, tenant_name: null, created_at: "2026-07-01T12:00:00Z",
    }
    jobs.byThreadId = { [THREAD]: job }
    renderPanel()

    await screen.findByText("Visits by district?")
    expect(screen.queryByTestId("chat-remote-turn")).toBeNull()
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "and by month?" } })
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled()
  })

  it("shows this tab's own turn running until its stream ends", async () => {
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

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "and by month?" } })
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })))
    await screen.findByText("By month: rising.")
    expect(useAppStore.getState().localTurnThreadIds.has(THREAD)).toBe(true)
    // A list refetched mid-turn reports it running.
    useAppStore.setState((state) => ({
      threads: state.threads.map((thread) => ({ ...thread, turn_running: true })),
    }))

    await act(async () => server.endReply())
    await vi.waitFor(() =>
      expect(useAppStore.getState().localTurnThreadIds.has(THREAD)).toBe(false),
    )
    // The turn's end refetches the list, whose flag shows the thread from here.
    await vi.waitFor(() => expect(useAppStore.getState().threads[0].turn_running).toBe(false))
    expect(server.detailPolls).toEqual([])
  })

  describe("this tab's own turn", () => {
    function listRunning(turnRunning: boolean) {
      useAppStore.setState({
        threads: [{
          id: THREAD, title: "Visits", title_is_custom: false, title_source: "generated",
          created_at: "2026-07-01T12:00:00Z", updated_at: "2026-07-01T12:00:00Z",
          last_viewed_at: null, turn_running: turnRunning,
        }],
      })
    }
    async function sendFollowUp() {
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "and by month?" } })
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })))
    }
    const local = () => useAppStore.getState().localTurnThreadIds.has(THREAD)

    it("ends when Stop aborts its stream", async () => {
      const server = mockServer()
      server.finish()
      renderPanel()
      await screen.findByText("Here are the visits.")
      await sendFollowUp()
      await screen.findByText("By month: rising.")
      expect(local()).toBe(true)

      await act(async () => fireEvent.click(screen.getByTestId("chat-stop")))

      await vi.waitFor(() => expect(local()).toBe(false))
    })

    it("ends on a refused send, leaving another tab's running turn listed", async () => {
      const server = mockServer()
      server.finish()
      server.chatReply = "busy"
      listRunning(true)
      renderPanel()
      await screen.findByText("Here are the visits.")
      // Another tab took the thread after this one loaded it.
      server.running = true
      await sendFollowUp()

      await vi.waitFor(() => expect(server.chatPosts).toBeGreaterThan(0))
      await vi.waitFor(() => expect(local()).toBe(false))
      expect(useAppStore.getState().threads[0].turn_running).toBe(true)
    })

    it("ends when the request never reaches the server, refetching the list", async () => {
      const server = mockServer()
      server.finish()
      server.chatReply = "network"
      listRunning(false)
      renderPanel()
      await screen.findByText("Here are the visits.")
      const listFetches = server.listFetches
      // Another tab takes the thread meanwhile.
      server.running = true
      await sendFollowUp()

      await vi.waitFor(() => expect(server.chatPosts).toBe(1))
      await vi.waitFor(() => expect(local()).toBe(false))
      await vi.waitFor(() => expect(server.listFetches).toBeGreaterThan(listFetches))
      await vi.waitFor(() => expect(useAppStore.getState().threads[0].turn_running).toBe(true))
    })

    it("goes to the server's flag, with one refetch, when the chat page unmounts mid-turn", async () => {
      const server = mockServer()
      server.finish()
      listRunning(false)
      const { unmount } = renderPanel()
      await screen.findByText("Here are the visits.")
      await sendFollowUp()
      await screen.findByText("By month: rising.")
      const listFetches = server.listFetches

      unmount()

      expect(local()).toBe(false)
      await vi.waitFor(() => expect(server.listFetches).toBe(listFetches + 1))
      await act(async () => server.endReply())
    })
  })

  it("checks at once when the tab becomes visible again", async () => {
    const server = mockServer()
    renderPanel()
    await screen.findByTestId("chat-remote-turn")
    Object.defineProperty(document, "hidden", { configurable: true, value: true })
    try {
      await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_POLL_MS))
      expect(server.detailPolls).toEqual([])
    } finally {
      Object.defineProperty(document, "hidden", { configurable: true, value: false })
    }
    server.finish()

    await act(async () => fireEvent(document, new Event("visibilitychange")))

    await vi.waitFor(() => expect(server.detailPolls).toEqual([THREAD]))
    expect(await screen.findByText("Here are the visits.")).toBeInTheDocument()
  })

  describe("its text, streamed as the server writes it", () => {
    it("shows the text as it arrives and reloads on the turn's done row", async () => {
      const server = mockServer()
      server.liveRows = [{ id: 1, run: "r1", text: "Visits rose ", done: false }]
      renderPanel()

      expect(await screen.findByTestId("resume-stream")).toHaveTextContent("Visits rose")
      expect(screen.queryByTestId("chat-remote-turn")).toBeNull()
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "and by month?" } })
      expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled()

      server.liveRows.push({ id: 2, run: "r1", text: "in March.", done: false })
      await act(() => vi.advanceTimersByTimeAsync(1_000))
      expect(screen.getByTestId("resume-stream")).toHaveTextContent("Visits rose in March.")

      // The server writes the done row once it has let go of the thread.
      server.finish()
      server.liveRows.push({ id: 3, run: "r1", text: "", done: true })
      await act(() => vi.advanceTimersByTimeAsync(1_000))

      expect(await screen.findByText("Here are the visits.")).toBeInTheDocument()
      await vi.waitFor(() => expect(screen.queryByTestId("resume-stream")).toBeNull())
      // The done row ended the wait before the thread poll had to.
      expect(server.detailPolls).toEqual([])
      expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled()
    })

    it("shows the placeholder until text arrives, and skips a run that already ended", async () => {
      const server = mockServer()
      server.liveRows = [{ id: 1, run: "earlier", text: "An earlier answer.", done: true }]
      renderPanel()

      await screen.findByTestId("chat-remote-turn")
      await act(() => vi.advanceTimersByTimeAsync(1_000))
      expect(screen.queryByTestId("resume-stream")).toBeNull()

      server.liveRows.push({ id: 2, run: "r2", text: "Visits rose.", done: false })
      await act(() => vi.advanceTimersByTimeAsync(3_000))
      expect(screen.getByTestId("resume-stream")).toHaveTextContent("Visits rose.")
      expect(screen.queryByTestId("chat-remote-turn")).toBeNull()
    })

    it("reloads when the next model call starts, so the last one comes from history", async () => {
      const server = mockServer()
      server.liveRows = [{ id: 1, run: "call-1", text: "Let me check.", done: false }]
      renderPanel()
      expect(await screen.findByTestId("resume-stream")).toHaveTextContent("Let me check.")
      const loads = server.messageLoads

      // The server reads after=0 from the latest run, which is now the second call.
      server.liveRows = [{ id: 2, run: "call-2", text: "Visits rose.", done: false }]
      await act(() => vi.advanceTimersByTimeAsync(2_000))

      await vi.waitFor(() => expect(server.messageLoads).toBe(loads + 1))
      await vi.waitFor(() =>
        expect(screen.getByTestId("resume-stream")).toHaveTextContent("Visits rose."),
      )
      expect(screen.getByTestId("resume-stream")).not.toHaveTextContent("Let me check.")
      expect(screen.getByTestId("chat-input")).toBeInTheDocument()
    })

    it("reloads when a call ends at its tool, then tails the next call", async () => {
      const server = mockServer()
      server.liveRows = [{ id: 1, run: "call-1", text: "Let me check.", done: false }]
      renderPanel()
      expect(await screen.findByTestId("resume-stream")).toHaveTextContent("Let me check.")
      const loads = server.messageLoads

      // The tool starts: the first call's run ends, and its message is in the history.
      server.liveRows.push({ id: 2, run: "call-1", text: "", done: true })
      await act(() => vi.advanceTimersByTimeAsync(2_000))
      await vi.waitFor(() => expect(server.messageLoads).toBe(loads + 1))
      await vi.waitFor(() => expect(screen.queryByTestId("resume-stream")).toBeNull())
      // Still running: the wait goes on, without showing the ended call twice.
      expect(await screen.findByTestId("chat-remote-turn")).toBeInTheDocument()

      server.liveRows.push({ id: 3, run: "call-2", text: "Visits rose.", done: false })
      await act(() => vi.advanceTimersByTimeAsync(2_000))
      expect(await screen.findByTestId("resume-stream")).toHaveTextContent("Visits rose.")
      expect(screen.getByTestId("resume-stream")).not.toHaveTextContent("Let me check.")
    })

    it("tails a remote turn no faster than the server writes it", async () => {
      const server = mockServer()
      server.liveRows = [{ id: 1, run: "r1", text: "Visits rose ", done: false }]
      renderPanel()
      await screen.findByTestId("resume-stream")
      const reads = server.tailReads

      await act(() => vi.advanceTimersByTimeAsync(3_000))

      expect(server.tailReads - reads).toBeLessThanOrEqual(3)
    })

    it("falls back to the thread poll when the turn wrote no done row", async () => {
      const server = mockServer()
      server.liveRows = [{ id: 1, run: "r1", text: "Visits rose ", done: false }]
      renderPanel()
      await screen.findByTestId("resume-stream")

      server.finish()
      await act(() => vi.advanceTimersByTimeAsync(REMOTE_TURN_POLL_MS))

      expect(await screen.findByText("Here are the visits.")).toBeInTheDocument()
      expect(server.detailPolls).toEqual([THREAD])
    })

    it("does not tail a thread whose turn is not running", async () => {
      const server = mockServer()
      server.finish()
      renderPanel()
      await screen.findByText("Here are the visits.")

      await act(() => vi.advanceTimersByTimeAsync(5_000))

      expect(server.tailReads).toBe(0)
    })
  })
})

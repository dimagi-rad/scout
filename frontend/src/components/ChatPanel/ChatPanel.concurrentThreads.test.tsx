import { createUIMessageStream, createUIMessageStreamResponse, type UIMessage } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { WorkspaceListItem } from "@/api/workspaces"
import type { PendingRequest } from "@/api/jobs"
import type { Thread } from "@/store/uiSlice"
import { ChatPanel } from "./ChatPanel"

const jobs = vi.hoisted(() => ({
  setPendingRequest: vi.fn(),
  recentlyCompleted: [] as string[],
}))

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: {},
    recentlyCompletedThreadIds: jobs.recentlyCompleted,
    recentTerminationsByToolCallId: {},
    pendingByThreadId: {},
    setPendingRequest: jobs.setPendingRequest,
    hidePendingRequest: vi.fn(),
    forgetPendingRequest: vi.fn(),
    refresh: vi.fn(),
    notifyJobLikelyStarted: vi.fn(),
  }),
}))

const WS = "11111111-1111-1111-1111-111111111111"
const WS_OTHER = "22222222-2222-2222-2222-222222222222"
const THREAD_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const THREAD_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
const A_PARTIAL = "Running recipe X, step one"
const A_FINAL = "Recipe X is done."
const B_HISTORY = "Earlier answer in chat B."
const B_REPLY = "Chat B answers while A runs."

function textMessage(id: string, role: "user" | "assistant", text: string): UIMessage {
  return { id, role, parts: [{ type: "text", text }] }
}

function listed(id: string, titleSource: Thread["title_source"] = "generated"): Thread {
  return {
    id, title: id, title_is_custom: false, title_source: titleSource,
    created_at: "2026-01-01", updated_at: "2026-01-01", last_viewed_at: null,
  }
}

function workspace(): WorkspaceListItem {
  return {
    id: WS, name: "Workspace", display_name: "Workspace", is_auto_created: false, role: "manage",
    tenants: [], member_count: 1, schema_status: "available", last_synced_at: null,
    created_at: "2026-01-01",
  }
}

const heldInA = {
  thread_id: THREAD_A,
  request_id: "r1",
  version: 1,
  parts: [{ id: "b-old", text: "held in A", added_at: "2026-10-02T00:00:00Z" }],
  state: "waiting",
  thread_job_id: "job-1",
  thread_job_state: "pending",
} satisfies PendingRequest

/** Chat A's turn streams its first words, then waits for ``finishA`` and ends as ``aEnds``. */
function mockServer({
  aEnds = "ok",
  holdB = false,
  holdHistoryOf = null,
}: {
  aEnds?: "ok" | "overload" | "error" | "held"
  holdB?: boolean
  /** Holds this thread's history load ("new": any other thread), answering with what
   *  was saved when it was asked. */
  holdHistoryOf?: string | null
} = {}) {
  let loadBHistory!: () => void
  const bHistoryGate = new Promise<void>((resolve) => (loadBHistory = resolve))
  let finishA!: () => void
  const aGate = new Promise<void>((resolve) => (finishA = resolve))
  let finishB!: () => void
  const bGate = new Promise<void>((resolve) => (finishB = resolve))
  const saved = new Map<string, UIMessage[]>([
    [THREAD_A, [textMessage("a-old", "assistant", "Chat A history.")]],
    [THREAD_B, [textMessage("b-old", "assistant", B_HISTORY)]],
  ])
  const chatThreads: string[] = []
  const messageLoads: string[] = []
  const heldHistories = new Map<string, Promise<void>>()
  /** Holds the thread's next history load until the returned release is called. */
  function holdNextHistory(threadId: string) {
    let release!: () => void
    heldHistories.set(threadId, new Promise<void>((resolve) => (release = resolve)))
    return () => release()
  }
  const threadLists = { count: 0, rows: [] as Thread[] }
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    if (url === "/api/chat/") {
      const body = JSON.parse(options?.body as string)
      const threadId: string = body.data.threadId
      chatThreads.push(threadId)
      const isA = threadId === THREAD_A
      return createUIMessageStreamResponse({
        stream: createUIMessageStream({
          execute: async ({ writer }) => {
            writer.write({ type: "start", messageId: `${threadId}-reply` })
            writer.write({ type: "text-start", id: "t" })
            if (isA) {
              writer.write({ type: "text-delta", id: "t", delta: A_PARTIAL })
              await aGate
              if (aEnds === "held") {
                // A part id that matches a message in B shows whether B's view hides it.
                writer.write({ type: "data-pending-request", data: heldInA, transient: true })
              }
              if (aEnds === "error") {
                writer.write({ type: "error", errorText: "Agent failed" })
                return
              }
              if (aEnds === "overload") {
                writer.write({
                  type: "data-chat-status",
                  data: { kind: "retryable-error", reason: "overloaded" },
                })
              }
              writer.write({ type: "text-delta", id: "t", delta: `. ${A_FINAL}` })
            } else {
              if (holdB) await bGate
              writer.write({ type: "text-delta", id: "t", delta: B_REPLY })
            }
            writer.write({ type: "text-end", id: "t" })
            writer.write({ type: "finish", finishReason: "stop" })
            saved.set(threadId, [
              ...(saved.get(threadId) ?? []),
              ...body.messages.slice(-1),
              textMessage(`${threadId}-reply`, "assistant", isA ? `${A_PARTIAL}. ${A_FINAL}` : B_REPLY),
            ])
          },
        }),
      })
    }
    const messages = url.match(/^\/api\/workspaces\/[^/]+\/threads\/([^/]+)\/messages\//)
    if (messages) {
      messageLoads.push(messages[1])
      const snapshot = saved.get(messages[1]) ?? []
      const heldHistory = heldHistories.get(messages[1])
      if (heldHistory) {
        heldHistories.delete(messages[1])
        await heldHistory
      }
      const isNew = messages[1] !== THREAD_A && messages[1] !== THREAD_B
      if (holdHistoryOf === messages[1] || (holdHistoryOf === "new" && isNew)) {
        holdHistoryOf = null
        await bHistoryGate
      }
      return Response.json(snapshot)
    }
    if (/\/threads\/[^/]+\/viewed\/$/.test(url)) return new Response(null, { status: 204 })
    if (/^\/api\/workspaces\/[^/]+\/threads\/$/.test(url)) {
      threadLists.count += 1
      return Response.json(threadLists.rows)
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  return {
    finishA, finishB, loadBHistory, holdNextHistory, chatThreads, messageLoads, threadLists,
  }
}

async function send(text: string) {
  await act(async () => {
    fireEvent.change(screen.getByRole("textbox"), { target: { value: text } })
    fireEvent.click(screen.getByRole("button", { name: "Send message" }))
  })
}

async function showThread(threadId: string) {
  await act(async () => {
    useAppStore.setState({ threadId })
  })
}

beforeEach(() => {
  localStorage.clear()
  jobs.recentlyCompleted = []
  // Selecting a workspace starts a fresh thread, so the thread is set after it.
  useAppStore.setState({ domains: [workspace()], domainsStatus: "loaded", activeDomainId: WS })
  useAppStore.setState({ threadId: THREAD_A, threads: [], threadsStatus: "loaded" })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("concurrent chat threads (#847)", () => {
  it("keeps a running turn in its own thread while another thread is used", async () => {
    const server = mockServer()
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")

    await send("run recipe X")
    await screen.findByText(A_PARTIAL)

    await showThread(THREAD_B)
    await screen.findByText(B_HISTORY)
    expect(screen.queryByText(A_PARTIAL, { exact: false })).toBeNull()
    expect(screen.queryByText("run recipe X")).toBeNull()
    // Chat B is idle, so it can send while A is still running.
    expect(screen.queryByRole("button", { name: "Stop response" })).toBeNull()

    await send("what about B?")
    await screen.findByText(B_REPLY)
    expect(server.chatThreads).toEqual([THREAD_A, THREAD_B])
    await waitFor(() => expect(server.threadLists.count).toBeGreaterThan(0))
    const listsBeforeAFinished = server.threadLists.count

    await act(async () => server.finishA())
    // A finished out of view still updates the sidebar.
    await waitFor(() => expect(server.threadLists.count).toBeGreaterThan(listsBeforeAFinished))
    // A's reply must not land in B when it finishes.
    expect(screen.queryByText(A_FINAL, { exact: false })).toBeNull()
    expect(screen.getByText(B_REPLY)).toBeInTheDocument()

    await showThread(THREAD_A)
    await screen.findByText(`${A_PARTIAL}. ${A_FINAL}`)
    expect(screen.queryByText(B_REPLY)).toBeNull()
  // Many round trips; under full-suite load it can pass the 5s default.
  }, 15_000)

  it("shows a still-running turn when its thread is shown again", async () => {
    const server = mockServer()
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")

    await send("run recipe X")
    await screen.findByText(A_PARTIAL)
    await showThread(THREAD_B)
    await screen.findByText(B_HISTORY)
    const loadsBeforeReturn = server.messageLoads.length

    await showThread(THREAD_A)
    expect(screen.getByText(A_PARTIAL)).toBeInTheDocument()
    expect(screen.getByText("run recipe X")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Stop response" })).toBeInTheDocument()
    // The live conversation is ahead of the server's, so it is not reloaded over.
    expect(server.messageLoads.slice(loadsBeforeReturn)).not.toContain(THREAD_A)

    await act(async () => server.finishA())
    await screen.findByText(`${A_PARTIAL}. ${A_FINAL}`)
  })

  it("does not retry the shown chat for an overload in a left chat", async () => {
    const server = mockServer({ aEnds: "overload", holdB: true })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")
    await send("run recipe X")
    await screen.findByText(A_PARTIAL)

    await showThread(THREAD_B)
    await screen.findByText(B_HISTORY)
    // A's overload lands mid-turn in B, so B's finish must not read it as its own.
    await send("what about B?")
    await act(async () => server.finishA())
    await act(async () => server.finishB())
    await screen.findByText(B_REPLY)

    // A retry would be a third POST; give it the chance to happen.
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(server.chatThreads).toEqual([THREAD_A, THREAD_B])
    expect(screen.queryByTestId("chat-overload-notice")).toBeNull()
  })

  it("does not show a left chat's failure when it is shown again", async () => {
    const server = mockServer({ aEnds: "error" })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")
    await send("run recipe X")
    await screen.findByText(A_PARTIAL)

    await showThread(THREAD_B)
    await screen.findByText(B_HISTORY)
    await act(async () => server.finishA())
    expect(screen.queryByTestId("chat-error")).toBeNull()

    const loadsBeforeReturn = server.messageLoads.length
    await showThread(THREAD_A)
    await screen.findByText("Chat A history.")
    // It reloads from the server, so a Retry on the old error would resend another turn.
    await waitFor(() =>
      expect(server.messageLoads.slice(loadsBeforeReturn)).toContain(THREAD_A),
    )
    expect(screen.queryByTestId("chat-error")).toBeNull()
  })

  it("starts a new chat ready to send while another is running", async () => {
    mockServer()
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")
    await send("run recipe X")
    await screen.findByText(A_PARTIAL)

    await act(async () => useAppStore.getState().uiActions.newThread())
    await screen.findByTestId("chat-input-prominent")
    expect(screen.queryByText(A_PARTIAL)).toBeNull()
    expect(screen.queryByRole("button", { name: "Stop response" })).toBeNull()
  })

  it("records a left chat's held request for its own thread without hiding the shown one's messages", async () => {
    const server = mockServer({ aEnds: "held" })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")
    await send("run recipe X")
    await screen.findByText(A_PARTIAL)

    await showThread(THREAD_B)
    await screen.findByText(B_HISTORY)
    jobs.setPendingRequest.mockClear()
    await act(async () => server.finishA())

    await waitFor(() =>
      expect(jobs.setPendingRequest).toHaveBeenCalledWith(THREAD_A, heldInA),
    )
    expect(screen.getByText(B_HISTORY)).toBeInTheDocument()
  })

  it("does not show a switched-to thread as a new chat while its history loads", async () => {
    const server = mockServer({ holdHistoryOf: THREAD_B })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")

    await showThread(THREAD_B)
    expect(screen.getByTestId("chat-history-loading")).toBeInTheDocument()
    expect(screen.queryByTestId("chat-input-prominent")).toBeNull()
    expect(screen.queryByText("Chat A history.")).toBeNull()

    await act(async () => server.loadBHistory())
    await screen.findByText(B_HISTORY)
  })

  it("keeps a turn left in another workspace out of the new one", async () => {
    const server = mockServer({ aEnds: "held" })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")
    await send("run recipe X")
    await screen.findByText(A_PARTIAL)

    await act(async () => {
      useAppStore.setState({ activeDomainId: WS_OTHER })
    })
    jobs.setPendingRequest.mockClear()
    const listsBefore = server.threadLists.count
    await act(async () => server.finishA())
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)))

    expect(jobs.setPendingRequest).not.toHaveBeenCalled()
    expect(server.threadLists.count).toBe(listsBefore)
    expect(screen.queryByText(A_PARTIAL, { exact: false })).toBeNull()
  })

  it("opens a new chat ready to type, and its history load never replaces a turn sent meanwhile", async () => {
    const server = mockServer({ holdB: true, holdHistoryOf: "new" })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")

    await act(async () => useAppStore.getState().uiActions.newThread())
    // Made up by this tab, so there is no history to wait for before typing.
    expect(screen.getByTestId("chat-input-prominent")).toBeInTheDocument()
    await send("hello C")
    await screen.findByText("hello C")

    // Its history, asked for before the send, lands mid-turn.
    await act(async () => server.loadBHistory())
    expect(screen.getByText("hello C")).toBeInTheDocument()

    await act(async () => server.finishB())
    await screen.findByText(B_REPLY)
    expect(screen.getByText("hello C")).toBeInTheDocument()
  })

  it("waits for the history of a thread the list does not show", async () => {
    // The list loads late and holds only the latest threads; a reload or deep link can
    // open a thread missing from it.
    const server = mockServer({ holdHistoryOf: THREAD_B })
    useAppStore.setState({ threads: [], threadsStatus: "loading" })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")

    await showThread(THREAD_B)
    expect(screen.getByTestId("chat-history-loading")).toBeInTheDocument()
    expect(screen.queryByTestId("chat-input-prominent")).toBeNull()

    await act(async () => server.loadBHistory())
    await screen.findByText(B_HISTORY)
  })

  it("starts no title polls for a left chat that finishes after the panel is gone", async () => {
    const server = mockServer()
    useAppStore.setState({ threads: [listed(THREAD_A, "first_message"), listed(THREAD_B)] })
    server.threadLists.rows = useAppStore.getState().threads
    const view = render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")
    await send("run recipe X")
    await screen.findByText(A_PARTIAL)
    await showThread(THREAD_B)
    await screen.findByText(B_HISTORY)

    view.unmount()
    const listsBefore = server.threadLists.count
    await act(async () => server.finishA())
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)))

    expect(server.threadLists.count).toBe(listsBefore)
  })
})

interface ScriptedReply {
  text: string
  overload?: boolean
  /** Holds the reply after it starts, until resolved. */
  gate?: Promise<void>
}

/** Each thread answers its turns in order from ``script``. */
function scriptedServer(script: Record<string, ScriptedReply[]>) {
  const chatThreads: string[] = []
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    if (url === "/api/chat/") {
      const threadId: string = JSON.parse(options?.body as string).data.threadId
      chatThreads.push(threadId)
      const reply = script[threadId].shift()!
      return createUIMessageStreamResponse({
        stream: createUIMessageStream({
          execute: async ({ writer }) => {
            writer.write({ type: "start" })
            writer.write({ type: "text-start", id: "t" })
            if (reply.gate) await reply.gate
            if (reply.overload) {
              writer.write({
                type: "data-chat-status",
                data: { kind: "retryable-error", reason: "overloaded" },
              })
            }
            writer.write({ type: "text-delta", id: "t", delta: reply.text })
            writer.write({ type: "text-end", id: "t" })
            writer.write({ type: "finish", finishReason: "stop" })
          },
        }),
      })
    }
    if (/\/messages\//.test(url)) return Response.json([])
    if (/\/threads\/[^/]+\/viewed\/$/.test(url)) return new Response(null, { status: 204 })
    if (/^\/api\/workspaces\/[^/]+\/threads\/$/.test(url)) return Response.json([])
    throw new Error(`Unexpected request: ${url}`)
  }))
  const turnsOf = (threadId: string) => chatThreads.filter((id) => id === threadId).length
  return { turnsOf }
}

function gate() {
  let open!: () => void
  const promise = new Promise<void>((resolve) => (open = resolve))
  return { promise, open }
}

describe("sending waits for the shown thread's history (#847)", () => {
  it("blocks Send while a finished resume reloads the thread, keeping the typed text", async () => {
    const server = mockServer()
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByText("Chat A history.")
    const release = server.holdNextHistory(THREAD_A)

    // A background resume of A finished, so A reloads its conversation.
    jobs.recentlyCompleted = [THREAD_A]
    await act(async () => useAppStore.setState({ threads: [listed(THREAD_A)] }))
    await waitFor(() =>
      expect(server.messageLoads.filter((id) => id === THREAD_A)).toHaveLength(2),
    )
    await act(async () => {
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "next question" } })
    })
    const sendButton = screen.getByRole("button", { name: "Send message" })
    expect(sendButton).toBeDisabled()
    await act(async () => {
      fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" })
    })
    expect(server.chatThreads).toEqual([])
    expect(screen.getByRole("textbox")).toHaveValue("next question")

    jobs.recentlyCompleted = []
    await act(async () => release())
    await waitFor(() => expect(sendButton).toBeEnabled())
    await act(async () => fireEvent.click(sendButton))
    expect(server.chatThreads).toEqual([THREAD_A])
  })
})

describe("overload retries per chat (#847)", () => {
  it("still gives a chat its retry while another chat's retry runs", async () => {
    const aRetry = gate()
    const server = scriptedServer({
      [THREAD_A]: [{ text: "A first", overload: true }, { text: "A retried", gate: aRetry.promise }],
      [THREAD_B]: [{ text: "B first", overload: true }, { text: "B retried" }],
    })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByTestId("chat-input-prominent")
    await send("in A")
    await waitFor(() => expect(server.turnsOf(THREAD_A)).toBe(2))

    await showThread(THREAD_B)
    await send("in B")
    await waitFor(() => expect(server.turnsOf(THREAD_B)).toBe(2))
    await screen.findByText("B retried")
    expect(screen.queryByTestId("chat-overload-notice")).toBeNull()
    await act(async () => aRetry.open())
  })

  it("does not retry a chat twice after another chat ends in its notice", async () => {
    const aRetry = gate()
    const server = scriptedServer({
      [THREAD_A]: [
        { text: "A first", overload: true },
        { text: "A retried", overload: true, gate: aRetry.promise },
        { text: "A third" },
      ],
      [THREAD_B]: [{ text: "B first", overload: true }, { text: "B retried", overload: true }],
    })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByTestId("chat-input-prominent")
    await send("in A")
    await waitFor(() => expect(server.turnsOf(THREAD_A)).toBe(2))

    await showThread(THREAD_B)
    await send("in B")
    await screen.findByTestId("chat-overload-notice")
    expect(server.turnsOf(THREAD_B)).toBe(2)

    await showThread(THREAD_A)
    await act(async () => aRetry.open())
    await screen.findByTestId("chat-overload-notice")
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(server.turnsOf(THREAD_A)).toBe(2)
  })
})


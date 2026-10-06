import { createUIMessageStream, createUIMessageStreamResponse, type UIMessage } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { WorkspaceListItem } from "@/api/workspaces"
import type { PendingRequest } from "@/api/jobs"
import { ChatPanel } from "./ChatPanel"

const jobs = vi.hoisted(() => ({ setPendingRequest: vi.fn() }))

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: {},
    recentlyCompletedThreadIds: [],
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
const THREAD_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const THREAD_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
const A_PARTIAL = "Running recipe X, step one"
const A_FINAL = "Recipe X is done."
const B_HISTORY = "Earlier answer in chat B."
const B_REPLY = "Chat B answers while A runs."

function textMessage(id: string, role: "user" | "assistant", text: string): UIMessage {
  return { id, role, parts: [{ type: "text", text }] }
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
function mockServer(
  { aEnds = "ok", holdB = false }: { aEnds?: "ok" | "overload" | "error" | "held"; holdB?: boolean } = {},
) {
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
  const threadLists = { count: 0 }
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
      return Response.json(saved.get(messages[1]) ?? [])
    }
    if (/\/threads\/[^/]+\/viewed\/$/.test(url)) return new Response(null, { status: 204 })
    if (/^\/api\/workspaces\/[^/]+\/threads\/$/.test(url)) {
      threadLists.count += 1
      return Response.json([])
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  return { finishA, finishB, chatThreads, messageLoads, threadLists }
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
  })

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

    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(server.chatThreads).toEqual([THREAD_A, THREAD_B])
    expect(screen.queryByTestId("chat-overload")).toBeNull()
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
})

import { createUIMessageStream, createUIMessageStreamResponse } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { busyTracker } from "@/api/busy"
import { ChatPanel } from "./ChatPanel"

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

// Backoff timing is covered in api/busy.test.ts; here only the sequence matters.
vi.mock("@/api/busy", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/busy")>()),
  busyRetryDelayMs: () => 0,
}))

const WS = "11111111-1111-1111-1111-111111111111"
const THREAD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const REPLY = "Here are your visits."
const BUSY_BODY = {
  error: "busy",
  code: "CAPACITY_EXHAUSTED",
  message: "Scout is busy right now. Please try again in a few seconds.",
}

function busyStreamResponse() {
  return createUIMessageStreamResponse({
    stream: createUIMessageStream({
      execute: ({ writer }) => {
        writer.write({ type: "start", messageId: crypto.randomUUID() })
        writer.write({
          type: "data-chat-status",
          data: { kind: "retryable-error", reason: "busy", retryAfter: 5 },
          transient: true,
        })
        writer.write({ type: "finish", finishReason: "stop" })
      },
    }),
  })
}

/** The busy part lands, the UI renders "streaming", and only later does the run finish. */
function busyThenSlowFinishResponse() {
  return createUIMessageStreamResponse({
    stream: createUIMessageStream({
      execute: async ({ writer }) => {
        // In one chunk with the first part, so onData runs before "streaming" renders.
        writer.write({
          type: "data-chat-status",
          data: { kind: "retryable-error", reason: "busy", retryAfter: 5 },
          transient: true,
        })
        writer.write({ type: "start", messageId: crypto.randomUUID() })
        await new Promise((resolve) => setTimeout(resolve, 50))
        writer.write({ type: "finish", finishReason: "stop" })
      },
    }),
  })
}

function replyResponse() {
  return createUIMessageStreamResponse({
    stream: createUIMessageStream({
      execute: ({ writer }) => {
        writer.write({ type: "start", messageId: crypto.randomUUID() })
        writer.write({ type: "text-start", id: "reply" })
        writer.write({ type: "text-delta", id: "reply", delta: REPLY })
        writer.write({ type: "text-end", id: "reply" })
        writer.write({ type: "finish", finishReason: "stop" })
      },
    }),
  })
}

function overloadThenHardErrorResponse() {
  return createUIMessageStreamResponse({
    stream: createUIMessageStream({
      execute: ({ writer }) => {
        writer.write({ type: "start", messageId: crypto.randomUUID() })
        writer.write({
          type: "data-chat-status",
          data: { kind: "retryable-error", reason: "overloaded" },
          transient: true,
        })
        writer.write({ type: "error", errorText: "An error occurred. Ref: abc123" })
      },
    }),
  })
}

function mockChat(busyAnswer: () => Response) {
  let busyLeft = Number.POSITIVE_INFINITY
  const chatPosts: string[] = []
  const messageLoads: string[] = []
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === "/api/chat/") {
      chatPosts.push(url)
      if (busyLeft > 0) {
        busyLeft -= 1
        return busyAnswer()
      }
      return replyResponse()
    }
    if (url.endsWith("/messages/?include=pending")) {
      messageLoads.push(url)
      return Response.json([])
    }
    if (url.endsWith("/viewed/")) return new Response(null, { status: 204 })
    if (url.endsWith("/canvas/")) return Response.json({ canvas: null, objects: [] })
    if (url.endsWith("/threads/")) return Response.json([])
    if (url.endsWith("/artifacts/")) return Response.json({ results: [] })
    throw new Error(`Unexpected request: ${url}`)
  }))
  return {
    chatPosts,
    messageLoads,
    recover: () => {
      busyLeft = 0
    },
  }
}

async function send(text: string) {
  await act(async () => {
    fireEvent.change(screen.getByRole("textbox"), { target: { value: text } })
    fireEvent.click(screen.getByRole("button", { name: "Send message" }))
  })
}

beforeEach(() => {
  localStorage.clear()
  useAppStore.setState({
    domains: [{
      id: WS, name: "W", display_name: "W", is_auto_created: false, role: "manage", tenants: [],
      member_count: 1, schema_status: "available", last_synced_at: null, created_at: "2026-01-01",
    }],
    domainsStatus: "loaded", activeDomainId: WS,
  })
  // Selecting the workspace made a new local chat; these tests are of a saved thread.
  useAppStore.setState({
    threadId: THREAD,
    threads: [], threadsStatus: "loaded", threadsAccessDenialReason: null, accessRetryOutcome: null,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// A busy 503 comes before the turn is checkpointed, so it gets the full retry
// budget; a busy stream part comes after, so it gets the overload path's one retry.
describe.each([
  ["a busy stream part", busyStreamResponse, 2],
  [
    "a busy 503",
    () => Response.json(BUSY_BODY, { status: 503, headers: { "Retry-After": "5" } }),
    4,
  ],
])("a chat turn answered with %s", (_label, busyAnswer, postsBeforeNotice) => {
  it("retries a few times, then offers a manual Retry that recovers", async () => {
    const api = mockChat(busyAnswer)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    // The saved-history load replaces messages, so let it land before sending.
    await waitFor(() => expect(api.messageLoads).toHaveLength(1))
    await act(async () => {})

    await send("How many visits last week?")

    const notice = await screen.findByTestId("chat-busy-notice")
    expect(notice).toHaveTextContent("Scout is busy right now")
    expect(api.chatPosts).toHaveLength(postsBeforeNotice)
    expect(screen.queryByTestId("chat-error")).toBeNull()
    expect(busyTracker.getSnapshot().retrying).toBe(0)

    api.recover()
    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-busy-retry"))
    })

    await screen.findByText(REPLY)
    expect(api.chatPosts).toHaveLength(postsBeforeNotice + 1)
    await waitFor(() => expect(screen.queryByTestId("chat-busy-notice")).toBeNull())
    consoleError.mockRestore()
  })

  it("drops the busy notice when the user opens another thread", async () => {
    const api = mockChat(busyAnswer)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await waitFor(() => expect(api.messageLoads).toHaveLength(1))
    await act(async () => {})
    await send("How many visits last week?")
    await screen.findByTestId("chat-busy-notice")

    await act(async () => {
      useAppStore.setState({ threadId: "cccccccc-cccc-cccc-cccc-cccccccccccc" })
    })

    await waitFor(() => expect(screen.queryByTestId("chat-busy-notice")).toBeNull())
    expect(api.chatPosts).toHaveLength(postsBeforeNotice)
    consoleError.mockRestore()
  })
})

describe("a turn that hits an overload and then fails hard", () => {
  it("keeps its error notice instead of being re-posted", async () => {
    const api = mockChat(overloadThenHardErrorResponse)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await waitFor(() => expect(api.messageLoads).toHaveLength(1))
    await act(async () => {})

    await send("How many visits last week?")

    await screen.findByTestId("chat-error")
    await act(async () => {})
    expect(api.chatPosts).toHaveLength(1)
    consoleError.mockRestore()
  })
})

describe("a busy turn whose retry then fails hard", () => {
  it("drops the retrying notice and shows the error", async () => {
    let posts = 0
    const api = mockChat(() => {
      posts += 1
      return posts === 1
        ? Response.json(BUSY_BODY, { status: 503 })
        : Response.json({ error: "Agent initialization failed" }, { status: 500 })
    })
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await waitFor(() => expect(api.messageLoads).toHaveLength(1))
    await act(async () => {})

    await send("How many visits last week?")

    await screen.findByTestId("chat-error")
    expect(api.chatPosts).toHaveLength(2)
    expect(busyTracker.getSnapshot().retrying).toBe(0)
    consoleError.mockRestore()
  })
})

describe("a busy stream part followed by a slow finish", () => {
  it("still retries once and then offers the busy notice", async () => {
    const api = mockChat(busyThenSlowFinishResponse)
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await waitFor(() => expect(api.messageLoads).toHaveLength(1))
    await act(async () => {})

    await send("How many visits last week?")

    await screen.findByTestId("chat-busy-notice", {}, { timeout: 3000 })
    expect(api.chatPosts).toHaveLength(2)
  })
})

describe("a stream-busy retry's budget", () => {
  it("is not refilled by leaving the thread and coming back mid-turn", async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    const chatPosts: number[] = []
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === "/api/chat/") {
        chatPosts.push(chatPosts.length)
        // The first post is busy at once; the retry is busy only after the user is back.
        if (chatPosts.length === 1) return busyStreamResponse()
        return createUIMessageStreamResponse({
          stream: createUIMessageStream({
            execute: async ({ writer }) => {
              writer.write({ type: "start", messageId: crypto.randomUUID() })
              await gate
              writer.write({
                type: "data-chat-status",
                data: { kind: "retryable-error", reason: "busy", retryAfter: 5 },
                transient: true,
              })
              writer.write({ type: "finish", finishReason: "stop" })
            },
          }),
        })
      }
      if (url.endsWith("/messages/?include=pending")) return Response.json([])
      if (url.endsWith("/viewed/")) return new Response(null, { status: 204 })
      if (url.endsWith("/canvas/")) return Response.json({ canvas: null, objects: [] })
      if (url.endsWith("/threads/")) return Response.json([])
      if (url.endsWith("/artifacts/")) return Response.json({ results: [] })
      throw new Error(`Unexpected request: ${url}`)
    }))
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await act(async () => {})
    await send("How many visits last week?")
    await waitFor(() => expect(chatPosts).toHaveLength(2))

    await act(async () => {
      useAppStore.setState({ threadId: "cccccccc-cccc-cccc-cccc-cccccccccccc" })
    })
    await act(async () => {
      useAppStore.setState({ threadId: THREAD })
    })
    await act(async () => release())

    await screen.findByTestId("chat-busy-notice", {}, { timeout: 3000 })
    expect(chatPosts).toHaveLength(2)
  })
})

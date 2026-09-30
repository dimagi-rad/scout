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
    if (url.endsWith("/messages/")) {
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
    domainsStatus: "loaded", activeDomainId: WS, threadId: THREAD,
    threads: [], threadsStatus: "loaded", threadsAccessLostMessage: null,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe.each([
  ["a busy stream part", busyStreamResponse],
  ["a busy 503", () => Response.json(BUSY_BODY, { status: 503, headers: { "Retry-After": "5" } })],
])("a chat turn answered with %s", (_label, busyAnswer) => {
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
    expect(api.chatPosts).toHaveLength(4)
    expect(screen.queryByTestId("chat-error")).toBeNull()
    expect(busyTracker.getSnapshot().retrying).toBe(0)

    api.recover()
    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-busy-retry"))
    })

    await screen.findByText(REPLY)
    expect(api.chatPosts).toHaveLength(5)
    await waitFor(() => expect(screen.queryByTestId("chat-busy-notice")).toBeNull())
    consoleError.mockRestore()
  })
})

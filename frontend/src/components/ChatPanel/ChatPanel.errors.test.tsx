import { createUIMessageStream, createUIMessageStreamResponse } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { ChatPanel } from "./ChatPanel"

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: {},
    recentlyCompletedThreadIds: [],
    recentTerminationsByToolCallId: {},
    notifyJobLikelyStarted: vi.fn(),
  }),
}))

const WS = "11111111-1111-1111-1111-111111111111"
const THREAD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const OTHER_THREAD = "cccccccc-cccc-cccc-cccc-cccccccccccc"
const OTHER_THREAD_MESSAGES = [
  { id: "u1", role: "user", parts: [{ type: "text", text: "Earlier question" }] },
  { id: "a1", role: "assistant", parts: [{ type: "text", text: "Earlier answer" }] },
]
const QUESTION ="How many visits last week?"
const REPLY = "Here are your visits."

const UNAVAILABLE_BODY = {
  error: "We couldn't confirm your access with the data provider. Try again shortly.",
  reason: "verification_unavailable",
  retryable: true,
  recovery_url: "/settings/connections",
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

function mockChat(answers: Array<() => Response>) {
  const chatBodies: Array<{ messages: Array<{ role: string; parts: unknown[] }> }> = []
  const messageLoads: string[] = []
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === "/api/chat/") {
      chatBodies.push(JSON.parse(String(init?.body)))
      const answer = answers.shift() ?? replyResponse
      return answer()
    }
    if (url.endsWith("/messages/")) {
      messageLoads.push(url)
      return Response.json(url.includes(OTHER_THREAD) ? OTHER_THREAD_MESSAGES : [])
    }
    if (url.endsWith("/viewed/")) return new Response(null, { status: 204 })
    if (url.endsWith("/canvas/")) return Response.json({ canvas: null, objects: [] })
    if (url.endsWith("/threads/")) return Response.json([])
    if (url.endsWith("/artifacts/")) return Response.json({ results: [] })
    throw new Error(`Unexpected request: ${url}`)
  }))
  return { chatBodies, messageLoads }
}

async function renderAndSend(api: ReturnType<typeof mockChat>) {
  render(<MemoryRouter><ChatPanel /></MemoryRouter>)
  await waitFor(() => expect(api.messageLoads).toHaveLength(1))
  await act(async () => {})
  await act(async () => {
    fireEvent.change(screen.getByRole("textbox"), { target: { value: QUESTION } })
    fireEvent.click(screen.getByRole("button", { name: "Send message" }))
  })
}

beforeEach(() => {
  localStorage.clear()
  vi.spyOn(console, "error").mockImplementation(() => {})
  useAppStore.setState({
    domains: [{
      id: WS, name: "W", display_name: "W", is_auto_created: false, role: "manage", tenants: [],
      member_count: 1, schema_status: "available", last_synced_at: null, created_at: "2026-01-01",
    }],
    domainsStatus: "loaded", activeDomainId: WS, threadId: THREAD,
    threads: [], threadsStatus: "loaded", threadsAccessDenialReason: null,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("a chat turn denied because access could not be confirmed", () => {
  it("explains it, does not auto-resend, and Retry resends the same message", async () => {
    const api = mockChat([() => Response.json(UNAVAILABLE_BODY, { status: 403 })])
    await renderAndSend(api)

    const notice = await screen.findByTestId("chat-error")
    expect(notice).toHaveAttribute("data-error-kind", "access-retry")
    expect(notice).toHaveTextContent("We couldn't confirm your access")
    await act(async () => {})
    expect(api.chatBodies).toHaveLength(1)

    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-error-retry"))
    })

    await screen.findByText(REPLY)
    expect(screen.queryByTestId("chat-error")).toBeNull()
    expect(api.chatBodies).toHaveLength(2)
    const resent = api.chatBodies[1].messages
    expect(resent.filter((m) => m.role === "user")).toHaveLength(1)
    expect(JSON.stringify(resent.at(-1))).toContain(QUESTION)
  })
})

describe("a retryable freshness denial sent as 503", () => {
  it("is classified by its reason, like the 403", async () => {
    const api = mockChat([() => Response.json(UNAVAILABLE_BODY, { status: 503 })])
    await renderAndSend(api)

    const notice = await screen.findByTestId("chat-error")
    expect(notice).toHaveAttribute("data-error-kind", "access-retry")
    expect(screen.getByTestId("chat-error-retry")).toBeInTheDocument()
    await act(async () => {})
    expect(api.chatBodies).toHaveLength(1)
  })
})

describe("a failed turn's notice after switching threads", () => {
  it("is dropped, so its Retry can never resend into the other thread", async () => {
    const api = mockChat([() => Response.json(UNAVAILABLE_BODY, { status: 403 })])
    await renderAndSend(api)
    await screen.findByTestId("chat-error-retry")

    await act(async () => {
      useAppStore.setState({ threadId: OTHER_THREAD })
    })

    await screen.findByText("Earlier answer")
    expect(screen.queryByTestId("chat-error")).toBeNull()
    expect(api.chatBodies).toHaveLength(1)
  })
})

describe("a chat turn denied because the credential expired", () => {
  it("shows the backend remedy with a Connected Accounts link and no Retry", async () => {
    const body = {
      error: "Your connection has expired. Reconnect it in Connected Accounts.",
      reason: "credential_expired",
      retryable: false,
      recovery_url: "/settings/connections",
    }
    const api = mockChat([() => Response.json(body, { status: 403 })])
    await renderAndSend(api)

    const notice = await screen.findByTestId("chat-error")
    expect(notice).toHaveTextContent(body.error)
    expect(screen.getByTestId("chat-error-recovery-link")).toHaveAttribute(
      "href",
      "/settings/connections",
    )
    expect(screen.queryByTestId("chat-error-retry")).toBeNull()
  })
})

describe("a chat turn that fails for an unknown reason", () => {
  it("shows the generic message and Retry recovers", async () => {
    const api = mockChat([
      () => Response.json({ error: "Agent initialization failed. Ref: abc" }, { status: 500 }),
    ])
    await renderAndSend(api)

    const notice = await screen.findByTestId("chat-error")
    expect(notice).toHaveAttribute("data-error-kind", "generic")
    expect(notice).not.toHaveTextContent("Agent initialization failed")

    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-error-retry"))
    })
    await screen.findByText(REPLY)
    expect(api.chatBodies).toHaveLength(2)
  })
})

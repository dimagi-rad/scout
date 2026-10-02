import { createUIMessageStream, createUIMessageStreamResponse, type UIMessage } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { PendingRequest } from "@/api/jobs"
import { WorkspaceJobsProvider } from "@/contexts/WorkspaceJobsContext"
import { ChatPanel } from "./ChatPanel"

const WS = "11111111-1111-1111-1111-111111111111"
const THREAD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const QUESTION = "How many visits last week?"
const FOLLOW_UP = "Only in Kenya"
const ANSWER = "There were 42 visits."

interface Server {
  pending: PendingRequest | null
  messages: UIMessage[]
  chatBodies: Record<string, unknown>[]
  partPosts: { id: string; text: string }[]
  /** Status the next parts/ POST answers with. */
  partStatus: number
  messageLoads: number
}

function request(parts: { id: string; text: string }[], overrides: Partial<PendingRequest> = {}) {
  return {
    thread_id: THREAD,
    request_id: "r1",
    version: parts.length,
    parts: parts.map((part) => ({ ...part, added_at: "2026-10-02T00:00:00Z" })),
    state: "waiting",
    thread_job_id: "job-1",
    thread_job_state: "pending",
    ...overrides,
  } satisfies PendingRequest
}

function heldResponse(pending: PendingRequest) {
  return createUIMessageStreamResponse({
    stream: createUIMessageStream({
      execute: ({ writer }) => {
        writer.write({ type: "start" })
        writer.write({ type: "data-pending-request", data: pending, transient: true })
        writer.write({ type: "finish", finishReason: "stop" })
      },
    }),
  })
}

function replyResponse(text: string) {
  return createUIMessageStreamResponse({
    stream: createUIMessageStream({
      execute: ({ writer }) => {
        writer.write({ type: "start", messageId: crypto.randomUUID() })
        writer.write({ type: "text-start", id: "reply" })
        writer.write({ type: "text-delta", id: "reply", delta: text })
        writer.write({ type: "text-end", id: "reply" })
        writer.write({ type: "finish", finishReason: "stop" })
      },
    }),
  })
}

function mockServer(): Server {
  const server: Server = {
    pending: null,
    messages: [],
    chatBodies: [],
    partPosts: [],
    partStatus: 200,
    messageLoads: 0,
  }
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    if (url === "/api/chat/") {
      const body = JSON.parse(options?.body as string)
      server.chatBodies.push(body)
      const last = body.messages.at(-1)
      const text = last.parts[0].text
      if (!body.data.pendingRequestVersion && server.pending === null && server.chatBodies.length === 1) {
        server.pending = request([{ id: last.id, text }], { thread_id: body.data.threadId })
        return heldResponse(server.pending)
      }
      server.pending = null
      return replyResponse(`echo: ${text}`)
    }
    if (url.endsWith("/pending-request/parts/")) {
      const part = JSON.parse(options?.body as string)
      server.partPosts.push(part)
      if (server.partStatus !== 200) {
        return Response.json({ error: "pending_request_conflict" }, { status: server.partStatus })
      }
      const parts = [...(server.pending?.parts ?? []), { ...part, added_at: "2026-10-02T00:00:01Z" }]
      server.pending = { ...server.pending!, parts, version: server.pending!.version + 1 }
      return Response.json(server.pending)
    }
    if (url.endsWith("/jobs/active/")) {
      return Response.json({
        jobs: [],
        workspace_loads: [],
        recent_terminations: [],
        pending_requests: server.pending ? { [server.pending.thread_id]: server.pending } : {},
      })
    }
    if (url.endsWith("/messages/?include=pending")) {
      server.messageLoads += 1
      return Response.json({ messages: server.messages, pending_request: server.pending })
    }
    if (url.endsWith("/viewed/")) return new Response(null, { status: 204 })
    if (url.endsWith("/threads/")) return Response.json([])
    return Response.json({}, { status: 404 })
  }))
  return server
}

function renderChat() {
  return render(
    <MemoryRouter>
      <WorkspaceJobsProvider workspaceId={WS}>
        <ChatPanel />
      </WorkspaceJobsProvider>
    </MemoryRouter>,
  )
}

async function type(text: string, button: string) {
  await act(async () => {
    fireEvent.change(screen.getByRole("textbox"), { target: { value: text } })
    fireEvent.click(screen.getByRole("button", { name: button }))
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
    threads: [], threadsStatus: "loaded", threadsAccessDenialReason: null, accessRetryOutcome: null,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("a message sent while the chat's data loads", () => {
  it("is held in a card that later parts join, then becomes the answered turn", async () => {
    const server = mockServer()
    renderChat()
    // The saved-history load replaces messages, so let it land before sending.
    await waitFor(() => expect(server.messageLoads).toBe(1))
    await act(async () => {})

    await type(QUESTION, "Send message")

    const card = await screen.findByTestId("pending-request-card")
    expect(within(card).getByTestId("pending-request-status")).toHaveTextContent("Waiting for data")
    expect(card).toHaveTextContent("Sent as one message when your data is ready")
    // The card shows the message; the transcript does not repeat it as a bubble.
    expect(screen.getAllByText(QUESTION)).toHaveLength(1)
    expect(screen.getByTestId("chat-input")).toHaveAttribute("placeholder", "Add to your request…")

    await type(FOLLOW_UP, "Add to request")

    await waitFor(() => expect(within(card).getByText(FOLLOW_UP)).toBeInTheDocument())
    expect(server.partPosts).toEqual([{ id: expect.any(String), text: FOLLOW_UP }])
    expect(server.chatBodies).toHaveLength(1)

    server.pending = null
    server.messages = [
      {
        id: `pr-${useAppStore.getState().threadId}-2`,
        role: "user",
        parts: [{ type: "text", text: `${QUESTION}\n\n${FOLLOW_UP}` }],
      },
      { id: "answer", role: "assistant", parts: [{ type: "text", text: ANSWER }] },
    ]

    // The next jobs poll (every 3s) finds the request gone and reloads the thread.
    await screen.findByText(ANSWER, undefined, { timeout: 8000 })
    expect(screen.queryByTestId("pending-request-card")).toBeNull()
    expect(screen.getByTestId("chat-input")).toHaveAttribute("placeholder", "Ask about your data...")
  }, 15_000)

  it("is sent as a normal turn when the request was claimed before it could join", async () => {
    const server = mockServer()
    renderChat()
    // The saved-history load replaces messages, so let it land before sending.
    await waitFor(() => expect(server.messageLoads).toBe(1))
    await act(async () => {})
    await type(QUESTION, "Send message")
    await screen.findByTestId("pending-request-card")

    server.partStatus = 409
    await type(FOLLOW_UP, "Add to request")

    await screen.findByText(`echo: ${FOLLOW_UP}`)
    expect(server.chatBodies).toHaveLength(2)
    expect(server.chatBodies[1].data).not.toHaveProperty("pendingRequestVersion")
  })

  it("offers Send now once its load ended without answering it, naming its version", async () => {
    const server = mockServer()
    server.pending = request([{ id: "p1", text: QUESTION }], {
      thread_id: useAppStore.getState().threadId,
      thread_job_state: "failed",
    })
    server.chatBodies.push({})
    renderChat()

    const card = await screen.findByTestId("pending-request-card")
    expect(within(card).getByTestId("pending-request-status")).toHaveTextContent("Couldn't answer")
    expect(screen.getByTestId("chat-input")).toHaveAttribute("placeholder", "Ask about your data...")

    await act(async () => {
      fireEvent.click(within(card).getByTestId("pending-request-send-now"))
    })

    await screen.findByText(`echo: ${QUESTION}`)
    expect(server.chatBodies.at(-1)?.data).toMatchObject({ pendingRequestVersion: 1 })
  })
})

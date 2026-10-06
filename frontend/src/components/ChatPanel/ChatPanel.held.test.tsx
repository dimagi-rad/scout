import { createUIMessageStream, createUIMessageStreamResponse, type UIMessage } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { PendingRequest } from "@/api/jobs"
import { WorkspaceJobsProvider } from "@/contexts/WorkspaceJobsContext"
import { ChatPanel } from "./ChatPanel"
import { readDraft } from "./draftStorage"

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
  /** Refuse a send that names a version, as when the request changed elsewhere. */
  refuseHeldSend: boolean
  /** Holds a held send's answer until resolved, so the test can act meanwhile. */
  heldSendGate: Promise<void> | null
  /** Answer a held send with a reply that fails after it began. */
  failHeldSendMidStream: boolean
  discards: number
  /** Refuse a held send as too long, as the server does past MAX_MESSAGE_LENGTH. */
  tooLongHeldSend: boolean
  patches: Record<string, unknown>[]
  /** What a background resume has streamed for the thread. */
  streamed: { id: number; run: string; text: string; done: boolean }[]
  /** Answer edits with 409 version, as when another tab changed the request. */
  editConflict: boolean
  /** Holds the saved-history load until resolved. */
  messagesGate: Promise<void> | null
  /** Answer a held send with a reply that starts and never ends, for Stop. */
  stallHeldReply: boolean
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

function stalledReplyResponse() {
  return createUIMessageStreamResponse({
    stream: createUIMessageStream({
      execute: async ({ writer }) => {
        writer.write({ type: "start", messageId: crypto.randomUUID() })
        writer.write({ type: "text-start", id: "reply" })
        writer.write({ type: "text-delta", id: "reply", delta: "Partial answer" })
        await new Promise(() => {})
      },
    }),
  })
}

function failingReplyResponse() {
  return createUIMessageStreamResponse({
    stream: createUIMessageStream({
      execute: ({ writer }) => {
        writer.write({ type: "start", messageId: crypto.randomUUID() })
        writer.write({ type: "text-start", id: "reply" })
        writer.write({ type: "text-delta", id: "reply", delta: "Sorry, something went wrong." })
        writer.write({ type: "text-end", id: "reply" })
        writer.write({ type: "error", errorText: "An error occurred. Ref: abc123" })
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
    refuseHeldSend: false,
    heldSendGate: null,
    failHeldSendMidStream: false,
    discards: 0,
    tooLongHeldSend: false,
    patches: [],
    streamed: [],
    editConflict: false,
    messagesGate: null,
    stallHeldReply: false,
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
      if (body.data.pendingRequestVersion && server.heldSendGate) await server.heldSendGate
      if (body.data.pendingRequestVersion && server.stallHeldReply) return stalledReplyResponse()
      if (body.data.pendingRequestVersion && server.failHeldSendMidStream) {
        return failingReplyResponse()
      }
      if (body.data.pendingRequestVersion && server.tooLongHeldSend) {
        return Response.json(
          { error: "Request too long — edit it", reason: "pending_request_too_long" },
          { status: 400 },
        )
      }
      if (body.data.pendingRequestVersion && server.refuseHeldSend) {
        return Response.json(
          { error: "pending_request_conflict", reason: "version" },
          { status: 409 },
        )
      }
      server.pending = null
      return replyResponse(`echo: ${text}`)
    }
    if (url.endsWith("/pending-request/") && options?.method === "PATCH") {
      const change = JSON.parse(options.body as string)
      server.patches.push(change)
      if (server.editConflict) {
        server.pending = { ...server.pending!, version: server.pending!.version + 1 }
        return Response.json(
          { error: "pending_request_conflict", reason: "version" },
          { status: 409 },
        )
      }
      const parts = change.text
        ? [{ id: "edited", text: change.text, added_at: "" }]
        : server.pending!.parts.filter((part) => part.id !== change.remove_part_id)
      server.pending = { ...server.pending!, parts, version: server.pending!.version + 1 }
      return Response.json(server.pending)
    }
    if (url.endsWith("/pending-request/") && options?.method === "DELETE") {
      server.discards += 1
      server.pending = null
      return Response.json({ status: "discarded" })
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
      if (server.messagesGate) await server.messagesGate
      return Response.json({ messages: server.messages, pending_request: server.pending })
    }
    if (url.includes("/resume-stream/")) {
      const after = Number(new URL(url, "http://x").searchParams.get("after") ?? 0)
      return Response.json({ chunks: server.streamed.filter((chunk) => chunk.id > after) })
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

function seedStore() {
  useAppStore.setState({
    domains: [{
      id: WS, name: "W", display_name: "W", is_auto_created: false, role: "manage", tenants: [],
      member_count: 1, schema_status: "available", last_synced_at: null, created_at: "2026-01-01",
    }],
    domainsStatus: "loaded", activeDomainId: WS, threadId: THREAD,
    threads: [], threadsStatus: "loaded", threadsAccessDenialReason: null, accessRetryOutcome: null,
  })
}

beforeEach(() => {
  localStorage.clear()
  seedStore()
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

  it("waits for the chat's history before Send now, then shows it once", async () => {
    const server = mockServer()
    server.pending = request([{ id: "p1", text: QUESTION }], {
      thread_id: useAppStore.getState().threadId,
      thread_job_state: "failed",
    })
    server.chatBodies.push({})
    server.messages = [{ id: "earlier", role: "assistant", parts: [{ type: "text", text: "Earlier answer." }] }]
    let loadHistory: () => void = () => {}
    server.messagesGate = new Promise((resolve) => (loadHistory = resolve))
    renderChat()

    const card = await screen.findByTestId("pending-request-card")
    // A turn racing the load would be duplicated or lost by it.
    expect(within(card).getByTestId("pending-request-send-now")).toBeDisabled()
    await act(async () => {
      fireEvent.click(within(card).getByTestId("pending-request-send-now"))
    })
    expect(server.chatBodies).toHaveLength(1)

    await act(async () => loadHistory())
    await screen.findByText("Earlier answer.")
    await waitFor(() =>
      expect(within(card).getByTestId("pending-request-send-now")).toBeEnabled(),
    )
    await act(async () => {
      fireEvent.click(within(card).getByTestId("pending-request-send-now"))
    })
    await screen.findByText(`echo: ${QUESTION}`)
    expect(screen.getAllByText("Earlier answer.")).toHaveLength(1)
    expect(screen.getAllByText(`echo: ${QUESTION}`)).toHaveLength(1)
  })

  it("keeps a stopped reply without reloading over it", async () => {
    const server = mockServer()
    server.pending = request([{ id: "p1", text: QUESTION }], {
      thread_id: useAppStore.getState().threadId,
      thread_job_state: "failed",
    })
    server.chatBodies.push({})
    server.messages = [{ id: "earlier", role: "assistant", parts: [{ type: "text", text: "Earlier answer." }] }]
    server.stallHeldReply = true
    renderChat()

    await screen.findByText("Earlier answer.")
    const card = await screen.findByTestId("pending-request-card")
    await waitFor(() =>
      expect(within(card).getByTestId("pending-request-send-now")).toBeEnabled(),
    )
    await act(async () => {
      fireEvent.click(within(card).getByTestId("pending-request-send-now"))
    })
    await screen.findByText("Partial answer")
    const loads = server.messageLoads

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Stop response" }))
    })
    await screen.findByTestId("chat-stopped-notice")
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(server.messageLoads).toBe(loads)
    expect(screen.getByText("Earlier answer.")).toBeInTheDocument()
    expect(screen.getByText("Partial answer")).toBeInTheDocument()
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
    expect(server.chatBodies.at(-1)?.data).toMatchObject({
      pendingRequestVersion: 1,
      pendingRequestId: "r1",
    })
  })

  it("puts the request and the text typed with it back when its send is refused", async () => {
    const server = mockServer()
    server.pending = request([{ id: "p1", text: QUESTION }], {
      thread_id: useAppStore.getState().threadId,
      thread_job_state: "failed",
    })
    server.chatBodies.push({})
    server.refuseHeldSend = true
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    renderChat()
    await screen.findByTestId("pending-request-card")

    await type(FOLLOW_UP, "Send message")

    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue(FOLLOW_UP))
    expect(server.chatBodies.at(-1)?.data).toMatchObject({ pendingRequestVersion: 1 })
    expect(await screen.findByTestId("pending-request-card")).toHaveTextContent(QUESTION)
    expect(screen.queryByText(`${QUESTION}\n\n${FOLLOW_UP}`)).toBeNull()
    // The card is the way on; an error notice's Retry would resend some other turn.
    expect(screen.queryByTestId("chat-error")).toBeNull()
    consoleError.mockRestore()
  })

  it("returns the typed text to its own chat's draft when refused after a switch", async () => {
    const server = mockServer()
    // Setting the user recreates the account's slices, so seed the workspace after it.
    useAppStore.setState({
      user: { id: "u1", email: "u@x", name: "U", is_staff: false, onboarding_complete: true },
    })
    seedStore()
    const sentFrom = useAppStore.getState().threadId
    server.pending = request([{ id: "p1", text: QUESTION }], {
      thread_id: sentFrom,
      thread_job_state: "failed",
    })
    server.chatBodies.push({})
    server.refuseHeldSend = true
    let release: () => void = () => {}
    server.heldSendGate = new Promise((resolve) => (release = resolve))
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    renderChat()
    await screen.findByTestId("pending-request-card")

    await type(FOLLOW_UP, "Send message")
    await waitFor(() => expect(server.chatBodies).toHaveLength(2))
    await act(async () => {
      useAppStore.setState({ threadId: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" })
    })
    await act(async () => release())

    await waitFor(() =>
      expect(readDraft({ userId: "u1", workspaceId: WS, threadId: sentFrom })).toBe(FOLLOW_UP),
    )
    consoleError.mockRestore()
  })

  it("keeps a held send whose reply failed mid-stream after a switch, and does not return its text", async () => {
    const server = mockServer()
    useAppStore.setState({
      user: { id: "u1", email: "u@x", name: "U", is_staff: false, onboarding_complete: true },
    })
    seedStore()
    const sentFrom = useAppStore.getState().threadId
    server.pending = request([{ id: "p1", text: QUESTION }], {
      thread_id: sentFrom,
      thread_job_state: "failed",
    })
    server.chatBodies.push({})
    server.failHeldSendMidStream = true
    let release: () => void = () => {}
    server.heldSendGate = new Promise((resolve) => (release = resolve))
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    renderChat()
    await screen.findByTestId("pending-request-card")

    await type(FOLLOW_UP, "Send message")
    await waitFor(() => expect(server.chatBodies).toHaveLength(2))
    await act(async () => {
      useAppStore.setState({ threadId: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" })
    })
    // The reply starts, then fails, while the chat is out of view: the server took it.
    await act(async () => release())
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)))

    expect(readDraft({ userId: "u1", workspaceId: WS, threadId: sentFrom })).toBe("")
    consoleError.mockRestore()
  })

  it("keeps a held send whose reply failed after it began, and does not return its text", async () => {
    const server = mockServer()
    server.pending = request([{ id: "p1", text: QUESTION }], {
      thread_id: useAppStore.getState().threadId,
      thread_job_state: "failed",
    })
    server.chatBodies.push({})
    server.failHeldSendMidStream = true
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    renderChat()
    await screen.findByTestId("pending-request-card")

    await type(FOLLOW_UP, "Send message")

    await screen.findByText("Sorry, something went wrong.")
    expect(screen.getByText(`${QUESTION} ${FOLLOW_UP}`, { normalizer: (t) => t.replace(/\s+/g, " ").trim() })).toBeInTheDocument()
    expect(screen.getByRole("textbox")).toHaveValue("")
    consoleError.mockRestore()
  })

  it("can be discarded while it waits", async () => {
    const server = mockServer()
    renderChat()
    await waitFor(() => expect(server.messageLoads).toBe(1))
    await act(async () => {})
    await type(QUESTION, "Send message")
    const card = await screen.findByTestId("pending-request-card")

    await act(async () => {
      fireEvent.click(within(card).getByTestId("pending-request-discard-waiting"))
    })

    await waitFor(() => expect(screen.queryByTestId("pending-request-card")).toBeNull())
    expect(server.discards).toBe(1)
  })

  it("says why a held send was refused when resending cannot help", async () => {
    const server = mockServer()
    server.pending = request([{ id: "p1", text: QUESTION }], {
      thread_id: useAppStore.getState().threadId,
      thread_job_state: "failed",
    })
    server.chatBodies.push({})
    server.tooLongHeldSend = true
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    renderChat()
    await screen.findByTestId("pending-request-card")

    await type(FOLLOW_UP, "Send message")

    expect(await screen.findByTestId("chat-error")).toHaveTextContent("Request too long — edit it")
    expect(screen.getByRole("textbox")).toHaveValue(FOLLOW_UP)
    expect(await screen.findByTestId("pending-request-card")).toHaveTextContent(QUESTION)
    consoleError.mockRestore()
  })

  it("shows a refusal that lands after a switch in neither chat", async () => {
    const server = mockServer()
    server.pending = request([{ id: "p1", text: QUESTION }], {
      thread_id: useAppStore.getState().threadId,
      thread_job_state: "failed",
    })
    server.chatBodies.push({})
    server.tooLongHeldSend = true
    let release: () => void = () => {}
    server.heldSendGate = new Promise((resolve) => (release = resolve))
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    renderChat()
    await screen.findByTestId("pending-request-card")

    await type(FOLLOW_UP, "Send message")
    await waitFor(() => expect(server.chatBodies).toHaveLength(2))
    server.messages = [
      { id: "earlier", role: "user", parts: [{ type: "text", text: "Earlier question" }] },
    ]
    await act(async () => {
      useAppStore.setState({ threadId: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" })
    })
    await screen.findByText("Earlier question")
    await act(async () => release())

    await waitFor(() => expect(server.chatBodies).toHaveLength(2))
    await act(async () => {})
    expect(screen.queryByTestId("chat-error")).toBeNull()
    consoleError.mockRestore()
  })

  it("removes a later part and rewrites the request through the card", async () => {
    const server = mockServer()
    renderChat()
    await waitFor(() => expect(server.messageLoads).toBe(1))
    await act(async () => {})
    await type(QUESTION, "Send message")
    await screen.findByTestId("pending-request-card")
    await type(FOLLOW_UP, "Add to request")
    const added = server.partPosts[0].id
    await screen.findByTestId(`pending-request-remove-${added}`)

    await act(async () => {
      fireEvent.click(screen.getByTestId(`pending-request-remove-${added}`))
    })
    await waitFor(() => expect(screen.queryByText(FOLLOW_UP)).toBeNull())
    expect(server.patches[0]).toEqual({ version: 2, remove_part_id: added })

    fireEvent.click(screen.getByTestId("pending-request-edit"))
    fireEvent.change(screen.getByTestId("pending-request-edit-text"), {
      target: { value: "Visits by week" },
    })
    await act(async () => {
      fireEvent.click(screen.getByTestId("pending-request-edit-save"))
    })

    expect(await screen.findByText("Visits by week")).toBeInTheDocument()
    expect(server.patches[1]).toEqual({ version: 3, text: "Visits by week" })
  })

  it("says the request was updated in another tab when an edit loses the race", async () => {
    const server = mockServer()
    renderChat()
    await waitFor(() => expect(server.messageLoads).toBe(1))
    await act(async () => {})
    await type(QUESTION, "Send message")
    await screen.findByTestId("pending-request-card")
    server.editConflict = true

    fireEvent.click(screen.getByTestId("pending-request-edit"))
    fireEvent.change(screen.getByTestId("pending-request-edit-text"), { target: { value: "mine" } })
    await act(async () => {
      fireEvent.click(screen.getByTestId("pending-request-edit-save"))
    })

    expect(await screen.findByTestId("pending-request-notice")).toHaveTextContent(
      "Updated in another tab",
    )
  })

  it("shows the answer as the resume writes it, until the conversation carries it", async () => {
    const server = mockServer()
    const thread = useAppStore.getState().threadId
    server.pending = request([{ id: "p1", text: QUESTION }], {
      thread_id: thread,
      state: "claimed",
      thread_job_state: "running",
    })
    server.chatBodies.push({})
    server.streamed = [{ id: 1, run: "r", text: "Counting the", done: false }]
    renderChat()

    expect(await screen.findByTestId("resume-stream")).toHaveTextContent("Counting the")
    server.streamed.push({ id: 2, run: "r", text: " visits now", done: false })
    await waitFor(() =>
      expect(screen.getByTestId("resume-stream")).toHaveTextContent("Counting the visits now"),
    )

    server.pending = null
    server.messages = [
      { id: `pr-r1-1`, role: "user", parts: [{ type: "text", text: QUESTION }] },
      { id: "answer", role: "assistant", parts: [{ type: "text", text: ANSWER }] },
    ]
    await screen.findByText(ANSWER, undefined, { timeout: 8000 })
    expect(screen.queryByTestId("resume-stream")).toBeNull()
  }, 15_000)
})

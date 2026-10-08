import { createUIMessageStream, createUIMessageStreamResponse } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { Thread } from "@/store/uiSlice"
import { workspaceApi, type WorkspaceListItem } from "@/api/workspaces"
import { Sidebar } from "@/components/Sidebar/Sidebar"
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

const WS = "11111111-1111-1111-1111-111111111111"
const QUESTION = "What are the module completion rates by district?"
const GENERATED = "Module completion by district"

const workspace: WorkspaceListItem = {
  id: WS, name: "Workspace", display_name: "Workspace", is_auto_created: false, role: "manage",
  tenants: [], member_count: 1, schema_status: "available", last_synced_at: null,
  created_at: "2026-01-01",
}

function thread(id: string, title: string, source: Thread["title_source"]): Thread {
  return {
    id, title, title_is_custom: false, title_source: source,
    created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z",
    last_viewed_at: "2026-10-01T00:00:00Z",
  }
}

/** The server lists the thread once the POST lands; ``generateTitle`` lets the worker write its title. */
function mockApi({ holdTurn = false } = {}) {
  let posted: string | null = null
  let titleGenerated = false
  let releaseTurn = () => {}
  const turnHeld = new Promise<void>((resolve) => {
    releaseTurn = resolve
  })
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    if (url === "/api/chat/") {
      posted = JSON.parse(options?.body as string).data.threadId
      return createUIMessageStreamResponse({
        stream: createUIMessageStream({
          execute: async ({ writer }) => {
            writer.write({ type: "start", messageId: crypto.randomUUID() })
            writer.write({ type: "text-start", id: "reply" })
            writer.write({ type: "text-delta", id: "reply", delta: "Here are the rates." })
            if (holdTurn) await turnHeld
            writer.write({ type: "text-end", id: "reply" })
            writer.write({ type: "finish", finishReason: "stop" })
          },
        }),
      })
    }
    if (url === `/api/workspaces/${WS}/threads/`) {
      if (!posted) return Response.json([])
      return Response.json([
        titleGenerated
          ? thread(posted, GENERATED, "generated")
          : thread(posted, QUESTION, "first_message"),
      ])
    }
    if (url.endsWith("/messages/?include=pending")) return Response.json([])
    if (url.endsWith("/viewed/")) return new Response(null, { status: 204 })
    throw new Error(`Unexpected request: ${url}`)
  }))
  return {
    generateTitle: () => {
      titleGenerated = true
    },
    releaseTurn,
  }
}

function renderChat() {
  render(
    // No routes: the sidebar navigates to the workspace's slugged chat path.
    <MemoryRouter initialEntries={[`/workspaces/${WS}/chat`]}>
      <Sidebar />
      <ChatPanel />
    </MemoryRouter>,
  )
}

async function send(text: string) {
  await act(async () => {
    fireEvent.change(await screen.findByRole("textbox"), { target: { value: text } })
    fireEvent.click(screen.getByRole("button", { name: "Send message" }))
  })
}

beforeEach(() => {
  localStorage.clear()
  useAppStore.getState().uiActions.newThread()
  vi.spyOn(workspaceApi, "list").mockResolvedValue([workspace])
  useAppStore.setState({
    domains: [workspace], domainsStatus: "loaded", activeDomainId: WS,
    threads: [], threadsStatus: "loaded", threadsAccessDenialReason: null, accessRetryOutcome: null,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("chat thread title", () => {
  it("shows the provisional title, then the generated title in header and sidebar", async () => {
    const server = mockApi()
    renderChat()

    await send(QUESTION)
    await screen.findByText("Here are the rates.")
    const threadRow = `sidebar-thread-${useAppStore.getState().threadId}`

    await waitFor(() => expect(screen.getByTestId("chat-thread-title")).toHaveTextContent(QUESTION))
    expect(screen.getByTestId(threadRow)).toHaveTextContent(QUESTION)

    server.generateTitle()
    await waitFor(
      () => expect(screen.getByTestId("chat-thread-title")).toHaveTextContent(GENERATED),
      { timeout: 5000 },
    )
    expect(screen.getByTestId(threadRow)).toHaveTextContent(GENERATED)
  }, 15000)

  it("lists a new chat as soon as its first message is sent, and reopens it mid-turn", async () => {
    const server = mockApi({ holdTurn: true })
    renderChat()
    const firstThread = useAppStore.getState().threadId

    await send(QUESTION)
    await screen.findByText("Here are the rates.")
    const row = await screen.findByTestId(`sidebar-thread-${firstThread}`)
    expect(row).toHaveTextContent(QUESTION)

    // Leave the running turn for a new chat: the first stays listed.
    await act(async () => fireEvent.click(screen.getByTestId("sidebar-new-chat")))
    await waitFor(() => expect(useAppStore.getState().threadId).not.toBe(firstThread))
    expect(screen.queryByText("Here are the rates.")).not.toBeInTheDocument()
    expect(screen.getByTestId(`sidebar-thread-${firstThread}`)).toHaveTextContent(QUESTION)

    await act(async () => fireEvent.click(screen.getByTestId(`sidebar-thread-${firstThread}`)))
    expect(useAppStore.getState().threadId).toBe(firstThread)
    expect(await screen.findByText("Here are the rates.")).toBeInTheDocument()

    await act(async () => server.releaseTurn())
    expect(screen.getByTestId(`sidebar-thread-${firstThread}`)).toHaveTextContent(QUESTION)
  }, 15000)
})

import { createUIMessageStream, createUIMessageStreamResponse } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
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

function thread(title: string, source: Thread["title_source"]): Thread {
  return {
    id: useAppStore.getState().threadId, title, title_is_custom: false, title_source: source,
    created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z",
    last_viewed_at: "2026-10-01T00:00:00Z",
  }
}

function mockApi() {
  let turnFinished = false
  let listCallsAfterTurn = 0
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === "/api/chat/") {
      return createUIMessageStreamResponse({
        stream: createUIMessageStream({
          execute: ({ writer }) => {
            writer.write({ type: "start", messageId: crypto.randomUUID() })
            writer.write({ type: "text-start", id: "reply" })
            writer.write({ type: "text-delta", id: "reply", delta: "Here are the rates." })
            writer.write({ type: "text-end", id: "reply" })
            writer.write({ type: "finish", finishReason: "stop" })
            turnFinished = true
          },
        }),
      })
    }
    if (url === `/api/workspaces/${WS}/threads/`) {
      if (!turnFinished) return Response.json([])
      // The worker has not written the generated title by the first refetch.
      listCallsAfterTurn += 1
      return Response.json([
        listCallsAfterTurn === 1 ? thread(QUESTION, "first_message") : thread(GENERATED, "generated"),
      ])
    }
    if (url.endsWith("/messages/")) return Response.json([])
    if (url.endsWith("/viewed/")) return new Response(null, { status: 204 })
    throw new Error(`Unexpected request: ${url}`)
  }))
}

beforeEach(() => {
  localStorage.clear()
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
    mockApi()
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WS}/chat`]}>
        <Routes>
          <Route
            path="/workspaces/:workspaceId/chat"
            element={<><Sidebar /><ChatPanel /></>}
          />
        </Routes>
      </MemoryRouter>,
    )

    await act(async () => {
      fireEvent.change(await screen.findByRole("textbox"), { target: { value: QUESTION } })
      fireEvent.click(screen.getByRole("button", { name: "Send message" }))
    })
    await screen.findByText("Here are the rates.")
    const threadRow = `sidebar-thread-${useAppStore.getState().threadId}`

    await waitFor(() => expect(screen.getByTestId("chat-thread-title")).toHaveTextContent(QUESTION))
    expect(screen.getByTestId(threadRow)).toHaveTextContent(QUESTION)

    await waitFor(
      () => expect(screen.getByTestId("chat-thread-title")).toHaveTextContent(GENERATED),
      { timeout: 5000 },
    )
    expect(screen.getByTestId(threadRow)).toHaveTextContent(GENERATED)
  }, 15000)
})

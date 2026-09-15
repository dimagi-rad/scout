import { createUIMessageStream, createUIMessageStreamResponse, type UIMessage } from "ai"
import { StrictMode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { createMemoryRouter, MemoryRouter, Outlet, Route, RouterProvider, Routes, useLocation } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { TenantMembership } from "@/store/domainSlice"
import { workspaceApi } from "@/api/workspaces"
import { Sidebar } from "@/components/Sidebar/Sidebar"
import { ChatPanel } from "./ChatPanel"
import { ChatRoute } from "./ChatRoute"
import type { CanvasProjection } from "./canvasApi"

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: {},
    recentlyCompletedThreadIds: [],
    recentTerminationsByToolCallId: {},
    notifyJobLikelyStarted: vi.fn(),
  }),
}))

const WS_A = "11111111-1111-1111-1111-111111111111"
const WS_B = "22222222-2222-2222-2222-222222222222"
const THREAD_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const THREAD_STALE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
const REPLY = "The dataset is saved."
const history: UIMessage[] = [{
  id: "saved-reply",
  role: "assistant",
  parts: [{ type: "text", text: REPLY }],
}]

function workspace(id: string, name: string): TenantMembership {
  return {
    id, name, display_name: name, is_auto_created: false, role: "manage", tenants: [],
    member_count: 1, schema_status: "available", last_synced_at: null, created_at: "2026-01-01",
  }
}

function projection(threadId: string): CanvasProjection {
  return {
    canvas: { id: "canvas", thread_id: threadId, status: "open", committed_at: null, updated_at: "" },
    objects: [{
      key: "dataset/smoke_intents", object_type: "dataset", object_uuid: "smoke-intents",
      change_type: "update", name: "smoke_intents", label: "Smoke intents", dataset: "",
      state: "unchanged", summary: "", diff: {}, fields: {}, base: {},
    }],
    diagnostics: [], pending_count: 0, can_commit: false,
  }
}

function chatResponse() {
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

function mockChatApi({ failFirst = false, saved = false } = {}) {
  const savedThreads = new Map<string, UIMessage[]>()
  if (saved) savedThreads.set(`${WS_A}/${THREAD_A}`, history)
  const sentContexts: { workspaceId: string; threadId: string }[] = []
  const canvasRequests: string[] = []
  const messageRequests: string[] = []
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    if (url === "/api/chat/") {
      const body = JSON.parse(options?.body as string)
      sentContexts.push(body.data)
      const key = `${body.data.workspaceId}/${body.data.threadId}`
      if (!savedThreads.has(key)) savedThreads.set(key, [])
      if (failFirst && sentContexts.length === 1) {
        return Response.json({ error: "Agent initialization failed" }, { status: 500 })
      }
      savedThreads.set(key, history)
      return chatResponse()
    }
    const threadRequest = url.match(/^\/api\/workspaces\/([^/]+)\/threads\/([^/]+)\/(messages|canvas|viewed)\/$/)
    if (threadRequest) {
      const [, workspaceId, threadId, resource] = threadRequest
      if (resource === "messages") {
        messageRequests.push(url)
        return Response.json(savedThreads.get(`${workspaceId}/${threadId}`) ?? [])
      }
      if (resource === "viewed") return new Response(null, { status: 204 })
      canvasRequests.push(url)
      // Canvas GET creates an owned shell even before the first chat POST.
      const key = `${workspaceId}/${threadId}`
      if (!savedThreads.has(key)) savedThreads.set(key, [])
      const canvas = projection(threadId)
      if (!savedThreads.get(key)?.length) canvas.objects = []
      return Response.json(canvas)
    }
    if (/^\/api\/workspaces\/[^/]+\/threads\/$/.test(url)) return Response.json([])
    throw new Error(`Unexpected request: ${url}`)
  }))
  return { sentContexts, canvasRequests, messageRequests }
}

function RouteContent({ sync }: { sync: boolean }) {
  const location = useLocation()
  return <>
    <div data-testid="chat-path">{location.pathname}</div>
    {sync ? <ChatRoute /> : <ChatPanel />}
  </>
}

function renderChat(initialPath: string, sync = true, coldStart = false) {
  const paths = [
    "/workspaces/:workspaceId/chat",
    "/workspaces/:workspaceId/chat/:threadId",
    "/workspaces/:slug/:workspaceId/chat",
    "/workspaces/:slug/:workspaceId/chat/:threadId",
  ]
  if (coldStart) {
    const router = createMemoryRouter([{
      element: <><Sidebar /><Outlet /></>,
      children: paths.map((path) => ({ path, element: <RouteContent sync={sync} /> })),
    }], { initialEntries: [initialPath] })
    return render(<StrictMode><RouterProvider router={router} /></StrictMode>)
  }
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        {paths.map((path) => <Route key={path} path={path} element={<RouteContent sync={sync} />} />)}
      </Routes>
    </MemoryRouter>,
  )
}

async function send(text: string) {
  await act(async () => {
    fireEvent.change(screen.getByRole("textbox"), { target: { value: text } })
    fireEvent.click(screen.getByRole("button", { name: "Send message" }))
  })
}

async function expectCanvas(api: ReturnType<typeof mockChatApi>, workspaceId: string, threadId: string) {
  fireEvent.click(screen.getByRole("button", { name: "Canvas" }))
  await screen.findByText("Smoke intents")
  expect(screen.queryByText("No conversation yet")).not.toBeInTheDocument()
  expect(api.canvasRequests.at(-1)).toBe(`/api/workspaces/${workspaceId}/threads/${threadId}/canvas/`)
}

beforeEach(() => {
  localStorage.clear()
  useAppStore.setState({
    domains: [workspace(WS_A, "Workspace A"), workspace(WS_B, "Workspace B")],
    domainsStatus: "loaded", activeDomainId: WS_A, threadId: THREAD_A,
    threads: [], threadsStatus: "loaded", threadsAccessLostMessage: null,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("chat thread identity", () => {
  it("opens a cold chat with Sidebar's deferred workspace fetch under StrictMode", async () => {
    useAppStore.setState({
      domains: [], domainsStatus: "idle", activeDomainId: null, threadId: crypto.randomUUID(),
    })
    const api = mockChatApi()
    let finishLoading!: (domains: TenantMembership[]) => void
    vi.spyOn(workspaceApi, "list").mockReturnValue(new Promise((resolve) => {
      finishLoading = resolve
    }))
    renderChat(`/workspaces/${WS_A}/chat`, true, true)
    await screen.findByTestId("chat-input-prominent")
    const freshThread = useAppStore.getState().threadId
    expect(useAppStore.getState().activeDomainId).toBe(WS_A)
    expect(useAppStore.getState().domainsStatus).toBe("loading")
    expect(screen.getByTestId("chat-path").textContent).toBe(
      `/workspaces/${WS_A}/chat/${freshThread}`,
    )

    await act(async () => {
      finishLoading([workspace(WS_B, "Workspace B"), workspace(WS_A, "Workspace A")])
    })
    const savedUrl = `/workspaces/workspace-a/${WS_A}/chat/${freshThread}`
    await waitFor(() => expect(screen.getByTestId("chat-path").textContent).toBe(savedUrl))
    await send("Say hello without tools or changes")
    await screen.findByText(REPLY)
    expect(api.sentContexts).toEqual([{ workspaceId: WS_A, threadId: freshThread }])
    expect(screen.getByTestId("chat-path").textContent).toBe(savedUrl)
    await expectCanvas(api, WS_A, freshThread)
  })

  it("keeps the same thread through an initialization failure, retry, Canvas, and reload", async () => {
    const api = mockChatApi({ failFirst: true })
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const view = renderChat(`/workspaces/${WS_A}/chat`)
    await waitFor(() => expect(api.messageRequests).toHaveLength(1))
    await send("Create a dataset")
    await screen.findByTestId("chat-error")
    expect(consoleError).toHaveBeenCalledWith("[Scout] Chat error:", expect.any(Error))
    await send("Try creating that dataset again")
    await screen.findByText(REPLY)
    await waitFor(() => expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument())
    expect(api.sentContexts).toEqual([
      { workspaceId: WS_A, threadId: THREAD_A },
      { workspaceId: WS_A, threadId: THREAD_A },
    ])
    const savedUrl = `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`
    expect(screen.getByTestId("chat-path")).toHaveTextContent(savedUrl)
    await expectCanvas(api, WS_A, THREAD_A)

    view.unmount()
    useAppStore.setState({ activeDomainId: WS_B, threadId: THREAD_STALE })
    renderChat(savedUrl)
    await screen.findByText(REPLY)
    expect(useAppStore.getState().threadId).toBe(THREAD_A)
    expect(useAppStore.getState().activeDomainId).toBe(WS_A)
    expect(screen.getByTestId("chat-path")).toHaveTextContent(savedUrl)
    await expectCanvas(api, WS_A, THREAD_A)
    expect(api.sentContexts).toHaveLength(2)
  })

  it.each(["", `/${THREAD_STALE}`])("uses the chat's thread for Canvas even with incomplete/stale URL params (%s)", async (suffix) => {
    const api = mockChatApi({ saved: true })
    renderChat(`/workspaces/workspace-a/${WS_A}/chat${suffix}`, false)
    await screen.findByText(REPLY)

    await expectCanvas(api, WS_A, THREAD_A)
    expect(api.canvasRequests).toEqual([`/api/workspaces/${WS_A}/threads/${THREAD_A}/canvas/`])
  })

  it("switches the transport, URL, and Canvas together when the workspace changes", async () => {
    const api = mockChatApi({ saved: true })
    renderChat(`/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`)
    await screen.findByText(REPLY)
    await expectCanvas(api, WS_A, THREAD_A)

    act(() => useAppStore.getState().domainActions.setActiveDomain(WS_B))
    await screen.findByTestId("chat-input-prominent")
    const nextThread = useAppStore.getState().threadId
    expect(nextThread).not.toBe(THREAD_A)
    await send("Create a dataset in this workspace")
    await screen.findByText(REPLY)
    expect(api.sentContexts).toEqual([{ workspaceId: WS_B, threadId: nextThread }])
    expect(screen.getByTestId("chat-path")).toHaveTextContent(
      `/workspaces/workspace-b/${WS_B}/chat/${nextThread}`,
    )
    await expectCanvas(api, WS_B, nextThread)
  })
})

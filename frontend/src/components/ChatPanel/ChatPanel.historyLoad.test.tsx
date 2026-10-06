import { createUIMessageStream, createUIMessageStreamResponse, type UIMessage } from "ai"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { newLocalThreadId } from "@/store/localThreads"
import type { WorkspaceListItem } from "@/api/workspaces"
import { ChatPanel } from "./ChatPanel"

vi.mock("./historyLoad", () => ({ HISTORY_LOAD_TIMEOUT_MS: 50 }))

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
const THREAD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const HISTORY: UIMessage[] = [{
  id: "saved", role: "assistant", parts: [{ type: "text", text: "Saved answer." }],
}]

function workspace(): WorkspaceListItem {
  return {
    id: WS, name: "Workspace", display_name: "Workspace", is_auto_created: false, role: "manage",
    tenants: [], member_count: 1, schema_status: "available", last_synced_at: null,
    created_at: "2026-01-01",
  }
}

/** History loads answer in order: "hold" until released, "stall" until aborted, or "ok". */
function mockServer(loads: ("hold" | "stall" | "ok")[]) {
  let release!: () => void
  const held = new Promise<void>((resolve) => (release = resolve))
  let finishReply!: () => void
  const reply = new Promise<void>((resolve) => (finishReply = resolve))
  const messageLoads: string[] = []
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    if (url === "/api/chat/") {
      return createUIMessageStreamResponse({
        stream: createUIMessageStream({
          execute: async ({ writer }) => {
            writer.write({ type: "start" })
            writer.write({ type: "text-start", id: "t" })
            writer.write({ type: "text-delta", id: "t", delta: "New reply." })
            await reply
            writer.write({ type: "text-end", id: "t" })
            writer.write({ type: "finish", finishReason: "stop" })
          },
        }),
      })
    }
    if (/\/threads\/[^/]+\/messages\//.test(url)) {
      const mode = loads.shift() ?? "ok"
      messageLoads.push(mode)
      if (mode === "hold") await held
      if (mode === "stall") {
        await new Promise((_, reject) => {
          options?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          )
        })
      }
      return Response.json(HISTORY)
    }
    if (/\/threads\/[^/]+\/viewed\/$/.test(url)) return new Response(null, { status: 204 })
    if (/\/threads\/$/.test(url)) return Response.json([])
    return Response.json({}, { status: 404 })
  }))
  return { release, finishReply, messageLoads }
}

function typeText(text: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } })
}

beforeEach(() => {
  localStorage.clear()
  useAppStore.setState({ domains: [workspace()], domainsStatus: "loaded", activeDomainId: WS })
  useAppStore.setState({ threadId: THREAD, threads: [], threadsStatus: "loaded" })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("a thread's history load", () => {
  it("shows the composer and a loading line while it loads, and sends once loaded", async () => {
    const server = mockServer(["hold"])
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    expect(await screen.findByTestId("chat-history-loading")).toBeInTheDocument()
    await act(async () => typeText("draft"))
    expect(screen.getByRole("textbox")).toHaveValue("draft")
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled()

    await act(async () => server.release())
    await screen.findByText("Saved answer.")
    expect(screen.queryByTestId("chat-history-loading")).toBeNull()
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled()
  })

  it("gives up on a stalled load, offers a retry, and loads on retry", async () => {
    const server = mockServer(["stall", "ok"])
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    const retry = await screen.findByTestId("chat-history-retry")
    expect(screen.queryByTestId("chat-history-loading")).toBeNull()
    // A failed load must not block sending for good.
    await act(async () => typeText("still here"))
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled()

    await act(async () => fireEvent.click(retry))
    await screen.findByText("Saved answer.")
    expect(screen.queryByTestId("chat-history-retry")).toBeNull()
    expect(server.messageLoads).toEqual(["stall", "ok"])
  })

  it("loads once a workspace arrives after the thread was set", async () => {
    mockServer(["ok"])
    useAppStore.setState({ activeDomainId: null })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    expect(screen.getByText("Select a domain to start chatting")).toBeInTheDocument()

    // Selecting a workspace starts a fresh thread; the URL then picks the saved one.
    await act(async () => useAppStore.setState({ activeDomainId: WS }))
    await act(async () => useAppStore.setState({ threadId: THREAD }))
    await screen.findByText("Saved answer.")
    await act(async () => typeText("hello"))
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled(),
    )
  })

  it("keeps a failed load's Retry through a turn sent meanwhile, then loads on Retry", async () => {
    const server = mockServer(["stall", "ok"])
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByTestId("chat-history-retry")

    await act(async () => typeText("hi"))
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Send message" })),
    )
    await screen.findByText("New reply.")
    // Mid-turn a reload would be skipped, leaving nothing to retry afterwards.
    expect(screen.getByTestId("chat-history-retry")).toBeDisabled()
    await act(async () => fireEvent.click(screen.getByTestId("chat-history-retry")))

    await act(async () => server.finishReply())
    const retry = await screen.findByTestId("chat-history-retry")
    await waitFor(() => expect(retry).toBeEnabled())
    expect(screen.getByText("Couldn't load earlier messages.")).toBeInTheDocument()
    await act(async () => fireEvent.click(retry))
    await screen.findByText("Saved answer.")
    expect(server.messageLoads).toEqual(["stall", "ok"])
  })

  it("explains the disabled Send button while loading", async () => {
    mockServer(["hold"])
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await screen.findByTestId("chat-history-loading")
    await act(async () => typeText("draft"))
    expect(screen.getByRole("button", { name: "Send message" }).closest("[title]"))
      .toHaveAttribute("title", "Loading the conversation...")
  })

  it("does not fetch history for a chat this tab just made up", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (/\/messages\//.test(url)) return Response.json({ error: "boom" }, { status: 500 })
      if (/\/viewed\/$/.test(url)) return new Response(null, { status: 204 })
      if (/\/threads\/$/.test(url)) return Response.json([])
      return Response.json({}, { status: 404 })
    })
    vi.stubGlobal("fetch", fetchMock)
    useAppStore.setState({ threadId: newLocalThreadId() })
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    await act(async () => typeText("hello"))

    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled()
    expect(screen.queryByTestId("chat-history-retry")).toBeNull()
    expect(screen.queryByText("Couldn't load earlier messages.")).toBeNull()
    expect(fetchMock.mock.calls.map(([url]) => String(url)).filter((url) => /\/messages\//.test(url)))
      .toEqual([])
  })
})

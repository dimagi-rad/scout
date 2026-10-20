import { createUIMessageStream, createUIMessageStreamResponse, type UIMessage } from "ai"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { workspaceApi, type WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { ChatPanel } from "./ChatPanel"

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: {},
    workspaceLoads: [],
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
const HISTORY: UIMessage[] = [
  { id: "a1", role: "assistant", parts: [{ type: "text", text: "Visits fell 20% in May." }] },
]

function mockApi() {
  const posted: string[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      if (url === "/api/chat/") {
        posted.push(options?.body as string)
        return createUIMessageStreamResponse({
          stream: createUIMessageStream({
            execute: async ({ writer }) => {
              writer.write({ type: "start", messageId: crypto.randomUUID() })
              writer.write({ type: "text-start", id: "reply" })
              writer.write({ type: "text-delta", id: "reply", delta: "Two sites closed." })
              writer.write({ type: "text-end", id: "reply" })
              writer.write({ type: "finish", finishReason: "stop" })
            },
          }),
        })
      }
      if (url.endsWith("/messages/?include=pending")) return Response.json(HISTORY)
      if (url.endsWith("/artifacts/")) return Response.json({ results: [] })
      if (url.endsWith("/threads/")) return Response.json([])
      if (url.endsWith("/viewed/")) return new Response(null, { status: 204 })
      throw new Error(`Unexpected request: ${url}`)
    }),
  )
  return posted
}

function selectText(node: Node, start: number, end: number) {
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, end)
  window.getSelection()!.removeAllRanges()
  window.getSelection()!.addRange(range)
  act(() => {
    document.dispatchEvent(new MouseEvent("mouseup"))
  })
}

const originalRect = Range.prototype.getBoundingClientRect
const originalElementRect = Element.prototype.getBoundingClientRect

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  Range.prototype.getBoundingClientRect = () => new DOMRect(100, 200, 80, 16)
  Element.prototype.getBoundingClientRect = () => new DOMRect(0, 0, 1000, 1000)
  // Its own update: a workspace change starts a new chat, which the next update replaces.
  useAppStore.setState({ activeDomainId: WS })
  useAppStore.setState({
    domains: [{ id: WS, role: "manage", tenants: [] } as unknown as WorkspaceListItem],
    domainsStatus: "loaded",
    threadId: THREAD,
    threads: [],
    threadsStatus: "loaded",
  })
  vi.spyOn(workspaceApi, "getFreshness").mockResolvedValue({ sources: [] } as never)
})

afterEach(() => {
  Range.prototype.getBoundingClientRect = originalRect
  Element.prototype.getBoundingClientRect = originalElementRect
  window.getSelection()?.removeAllRanges()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("ChatPanel quote-reply (#920)", () => {
  it("quotes selected reply text into the composer and sends it with the reply", async () => {
    const posted = mockApi()
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    const answer = (await screen.findByText("Visits fell 20% in May.")).firstChild!
    selectText(answer, 0, 11)
    fireEvent.click(screen.getByTestId("chat-quote-action"))
    selectText(answer, 12, 15)
    fireEvent.click(screen.getByTestId("chat-quote-action"))

    const chips = screen.getAllByTestId("composer-quote-chip")
    expect(chips.map((c) => c.textContent)).toEqual(["Visits fell", "20%"])
    expect(screen.getByTestId("chat-input")).toHaveFocus()

    await act(async () => {
      fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "Why?" } })
      fireEvent.click(screen.getByTestId("chat-send"))
    })

    await screen.findByText("Two sites closed.")
    expect(posted).toHaveLength(1)
    expect(posted[0]).toContain(JSON.stringify("> Visits fell\n\n> 20%\n\nWhy?"))
    expect(screen.queryByTestId("composer-quote-chip")).not.toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getAllByTestId("chat-message-quote").map((q) => q.textContent)).toEqual([
        "Visits fell",
        "20%",
      ]),
    )
  })

  it("removes a quote with its ×", async () => {
    mockApi()
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    const answer = (await screen.findByText("Visits fell 20% in May.")).firstChild!
    selectText(answer, 0, 11)
    fireEvent.click(screen.getByTestId("chat-quote-action"))
    fireEvent.click(screen.getByTestId("composer-quote-remove"))

    expect(screen.queryByTestId("composer-quote-chip")).not.toBeInTheDocument()
  })

  it("keeps quotes with the chat they were picked in", async () => {
    mockApi()
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)

    const answer = (await screen.findByText("Visits fell 20% in May.")).firstChild!
    selectText(answer, 0, 11)
    fireEvent.click(screen.getByTestId("chat-quote-action"))
    expect(screen.getByTestId("composer-quote-chip")).toBeInTheDocument()

    act(() => useAppStore.setState({ threadId: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" }))
    await screen.findByText("Visits fell 20% in May.")
    expect(screen.queryByTestId("composer-quote-chip")).not.toBeInTheDocument()
  })
})

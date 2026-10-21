import { createUIMessageStream, createUIMessageStreamResponse, type UIMessage } from "ai"
import { act, createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { uploadChatAttachment, type UploadedAttachment } from "@/api/chatAttachments"
import { workspaceApi, type WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { ChatComposer } from "./ChatComposer"
import { ChatPanel } from "./ChatPanel"
import { TopBarProvider } from "@/components/TopBar"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import type { ChatAttachments } from "./attachments/useChatAttachments"

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

vi.mock("@/api/chatAttachments", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/chatAttachments")>()),
  uploadChatAttachment: vi.fn(),
}))

// jsdom has no createImageBitmap; resizing is covered where it runs, in the browser.
vi.mock("./attachments/downscale", () => ({ downscaleImage: async (file: File) => file }))

const WS = "11111111-1111-1111-1111-111111111111"
const THREAD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const ATTACHMENT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
const HISTORY: UIMessage[] = [
  { id: "a1", role: "assistant", parts: [{ type: "text", text: "Visits fell 20% in May." }] },
]

const upload = vi.mocked(uploadChatAttachment)

function uploaded(file: File): UploadedAttachment {
  return {
    id: ATTACHMENT_ID,
    filename: file.name,
    mediaType: file.type,
    size: file.size,
    url: `/api/chat/attachments/${ATTACHMENT_ID}/`,
  }
}

function mockApi(history: UIMessage[] = HISTORY) {
  const posted: { messages: UIMessage[] }[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      if (url === "/api/chat/") {
        posted.push(JSON.parse(options?.body as string))
        return createUIMessageStreamResponse({
          stream: createUIMessageStream({
            execute: async ({ writer }) => {
              writer.write({ type: "start", messageId: crypto.randomUUID() })
              writer.write({ type: "text-start", id: "reply" })
              writer.write({ type: "text-delta", id: "reply", delta: "Got the screenshot." })
              writer.write({ type: "text-end", id: "reply" })
              writer.write({ type: "finish", finishReason: "stop" })
            },
          }),
        })
      }
      if (url.endsWith("/messages/?include=pending")) return Response.json(history)
      if (url.endsWith("/artifacts/")) return Response.json({ results: [] })
      if (url.endsWith("/threads/")) return Response.json([])
      if (url.endsWith("/viewed/")) return new Response(null, { status: 204 })
      throw new Error(`Unexpected request: ${url}`)
    }),
  )
  return posted
}

function fileDrag(files: File[]) {
  return { dataTransfer: { types: ["Files"], files, dropEffect: "none" } }
}

const textDrag = { dataTransfer: { types: ["text/plain", "text/html"], files: [], dropEffect: "none" } }

function screenshot(name = "shot.png") {
  return new File([new Uint8Array([137, 80, 78, 71])], name, { type: "image/png" })
}

/** ChatPanel inside AppLayout's shape: the drop must work on its siblings too. */
function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-screen">
      <aside data-testid="sidebar-shell">Sidebar</aside>
      <div className="flex flex-1 flex-col">
        <TopBarProvider>
          <main>{children}</main>
        </TopBarProvider>
      </div>
      {/* The real artifact panel is a modal Radix dialog, which sets pointer-events:
          none on body while open. */}
      <Dialog open>
        <DialogContent data-testid="artifact-modal" showCloseButton={false}>
          <DialogTitle>Artifact</DialogTitle>
          <DialogDescription>Artifact preview</DialogDescription>
          <iframe title="artifact" />
        </DialogContent>
      </Dialog>
    </div>
  )
}

async function renderThread() {
  const posted = mockApi()
  render(<MemoryRouter><AppShell><ChatPanel /></AppShell></MemoryRouter>)
  await screen.findByText("Visits fell 20% in May.")
  return posted
}

// jsdom has no DragEvent, so without this a drag event loses its coordinates
// and relatedTarget, which tell leaving the window from crossing an element.
class TestDragEvent extends MouseEvent {
  dataTransfer: DataTransfer | null
  constructor(type: string, init: DragEventInit = {}) {
    super(type, init)
    this.dataTransfer = init.dataTransfer ?? null
  }
}

beforeEach(() => {
  vi.stubGlobal("DragEvent", TestDragEvent)
  sessionStorage.clear()
  localStorage.clear()
  useAppStore.setState({ activeDomainId: WS })
  useAppStore.setState({
    domains: [{ id: WS, role: "manage", tenants: [] } as unknown as WorkspaceListItem],
    domainsStatus: "loaded",
    threadId: THREAD,
    threads: [],
    threadsStatus: "loaded",
  })
  vi.spyOn(workspaceApi, "getFreshness").mockResolvedValue({ sources: [] } as never)
  upload.mockReset()
  upload.mockImplementation(async (file) => uploaded(file))
  URL.createObjectURL = vi.fn(() => "blob:preview")
  URL.revokeObjectURL = vi.fn()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("ChatPanel attachments (#913)", () => {
  it("attaches a file dropped on the thread header and sends it as a file part", async () => {
    const posted = await renderThread()
    const header = screen.getByTestId("chat-thread-title")
    const file = screenshot()

    fireEvent.dragEnter(header, fileDrag([file]))
    expect(screen.getByTestId("chat-drop-overlay")).toHaveTextContent("Drop files to attach")
    await act(async () => {
      fireEvent.drop(header, fileDrag([file]))
    })

    expect(screen.queryByTestId("chat-drop-overlay")).toBeNull()
    expect(upload).toHaveBeenCalledWith(file, WS, expect.any(Function), expect.any(AbortSignal))
    await waitFor(() =>
      expect(screen.getByTestId("chat-attachment-chip-0")).toHaveAttribute("data-status", "ready"),
    )

    // Attachments alone are enough to send.
    expect(screen.getByTestId("chat-send")).not.toBeDisabled()
    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-send"))
    })

    await screen.findByText("Got the screenshot.")
    expect(posted).toHaveLength(1)
    const sent = posted[0].messages.at(-1)!
    expect(sent.parts).toContainEqual({
      type: "file",
      mediaType: "image/png",
      filename: "shot.png",
      url: `/api/chat/attachments/${ATTACHMENT_ID}/`,
    })
    expect(screen.queryByTestId("chat-attachment-chip-0")).toBeNull()
    const shown = screen.getByTestId("chat-message-attachment")
    expect(shown).toHaveAttribute("href", `/api/chat/attachments/${ATTACHMENT_ID}/`)
    expect(shown.querySelector("img")).toHaveAttribute("src", `/api/chat/attachments/${ATTACHMENT_ID}/`)
  })

  it("sends typed text and the files together", async () => {
    const posted = await renderThread()
    await act(async () => {
      fireEvent.change(screen.getByTestId("chat-attach-input"), {
        target: { files: [new File(["{}"], "registry.json", { type: "application/json" })] },
      })
    })
    await waitFor(() =>
      expect(screen.getByTestId("chat-attachment-chip-0")).toHaveAttribute("data-status", "ready"),
    )
    await act(async () => {
      fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "What's in this?" } })
      fireEvent.click(screen.getByTestId("chat-send"))
    })
    await screen.findByText("Got the screenshot.")
    const parts = posted[0].messages.at(-1)!.parts
    expect(parts).toContainEqual({ type: "text", text: "What's in this?" })
    expect(parts).toContainEqual(expect.objectContaining({ type: "file", filename: "registry.json" }))
  })

  it("asks for a reply rather than sending quotes with files alone", () => {
    const attachments: ChatAttachments = {
      items: [{ localId: "a", filename: "shot.png", kind: "image", size: 4, status: "ready", progress: 1 }],
      busy: false,
      failed: false,
      ready: true,
      notice: null,
      add: vi.fn(),
      remove: vi.fn(),
      take: vi.fn(() => []),
      restore: vi.fn(),
    }
    const onSend = vi.fn()
    const { rerender } = render(
      <ChatComposer input="" setInput={vi.fn()} onSend={onSend} attachments={attachments} quotes={["a point"]} />,
    )
    expect(screen.getByTestId("chat-send")).toBeDisabled()
    expect(screen.getByTestId("chat-send").parentElement).toHaveAttribute(
      "title",
      "Type a reply to go with the quotes",
    )
    rerender(<ChatComposer input="" setInput={vi.fn()} onSend={onSend} attachments={attachments} />)
    expect(screen.getByTestId("chat-send")).not.toBeDisabled()
  })

  it("ignores a drag that carries no files, such as a text selection", async () => {
    await renderThread()
    const header = screen.getByTestId("chat-thread-title")

    fireEvent.dragEnter(header, textDrag)
    expect(screen.queryByTestId("chat-drop-overlay")).toBeNull()
    const over = createEvent.dragOver(header, textDrag)
    fireEvent(header, over)
    expect(over.defaultPrevented).toBe(false)
    fireEvent.drop(header, textDrag)

    expect(upload).not.toHaveBeenCalled()
    expect(screen.queryByTestId("chat-attachment-chip-0")).toBeNull()
  })

  it.each([
    ["the app top bar", "top-bar"],
    ["the sidebar", "sidebar-shell"],
    ["the artifact panel", "artifact-modal"],
  ])("takes a file dropped on %s, outside the chat panel", async (_where, testId) => {
    await renderThread()
    const target = screen.getByTestId(testId)
    const file = screenshot()

    fireEvent.dragEnter(target, fileDrag([file]))
    const overlay = screen.getByTestId("chat-drop-overlay")
    // Full window, and it takes the pointer even under the modal's
    // body { pointer-events: none }, so the artifact iframe can't swallow the drop.
    expect(overlay).toHaveClass("fixed", "inset-0")
    expect(document.body.style.pointerEvents).toBe("none")
    expect(getComputedStyle(overlay).pointerEvents).toBe("auto")
    const over = createEvent.dragOver(target, fileDrag([file]))
    fireEvent(target, over)
    expect(over.defaultPrevented).toBe(true)

    await act(async () => {
      fireEvent.drop(target, fileDrag([file]))
    })
    expect(screen.queryByTestId("chat-drop-overlay")).toBeNull()
    expect(upload).toHaveBeenCalledWith(file, WS, expect.any(Function), expect.any(AbortSignal))
  })

  it("takes a drop on the overlay itself", async () => {
    await renderThread()
    const file = screenshot()
    fireEvent.dragEnter(screen.getByTestId("top-bar"), fileDrag([file]))
    await act(async () => {
      fireEvent.drop(screen.getByTestId("chat-drop-overlay"), fileDrag([file]))
    })
    expect(upload).toHaveBeenCalledTimes(1)
  })

  it("keeps the overlay while the drag moves inside the window, and drops it on leaving", async () => {
    await renderThread()
    const header = screen.getByTestId("chat-thread-title")
    const drag = fileDrag([screenshot()])

    fireEvent.dragEnter(header, drag)
    const overlay = screen.getByTestId("chat-drop-overlay")
    // Moving onto the overlay leaves the header for something inside the window.
    fireEvent.dragEnter(overlay, drag)
    fireEvent.dragLeave(header, { ...drag, relatedTarget: overlay, clientX: 300, clientY: 200 })
    expect(screen.getByTestId("chat-drop-overlay")).toBeInTheDocument()

    fireEvent.dragLeave(overlay, { ...drag, relatedTarget: null, clientX: 0, clientY: 0 })
    expect(screen.queryByTestId("chat-drop-overlay")).toBeNull()
  })

  it("does not stick when the hovered node unmounts mid-drag", async () => {
    await renderThread()
    const drag = fileDrag([screenshot()])
    const doomed = document.createElement("div")
    document.body.append(doomed)

    fireEvent.dragEnter(doomed, drag)
    doomed.remove()
    // Its dragleave goes to a detached node and never reaches the window.
    fireEvent.dragLeave(doomed, drag)
    fireEvent.dragEnter(screen.getByTestId("chat-input"), drag)
    fireEvent.dragLeave(screen.getByTestId("chat-input"), {
      ...drag,
      relatedTarget: screen.getByTestId("chat-drop-overlay"),
      clientX: 300,
      clientY: 200,
    })
    expect(screen.getByTestId("chat-drop-overlay")).toBeInTheDocument()

    // The drag is over (it ended outside the page): the pointer moving again says so.
    fireEvent.pointerMove(window)
    expect(screen.queryByTestId("chat-drop-overlay")).toBeNull()
  })

  it("drops the overlay when dragover stops arriving", async () => {
    await renderThread()
    vi.useFakeTimers()
    try {
      fireEvent.dragEnter(screen.getByTestId("top-bar"), fileDrag([screenshot()]))
      expect(screen.getByTestId("chat-drop-overlay")).toBeInTheDocument()
      act(() => {
        vi.advanceTimersByTime(4_100)
      })
      expect(screen.queryByTestId("chat-drop-overlay")).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it("says a dropped folder can't be attached instead of uploading it", async () => {
    await renderThread()
    const folder = new File([], "screenshots")
    const dataTransfer = {
      types: ["Files"],
      files: [folder],
      items: [{ kind: "file", webkitGetAsEntry: () => ({ isDirectory: true }) }],
      dropEffect: "none",
    }
    await act(async () => {
      fireEvent.drop(screen.getByTestId("top-bar"), { dataTransfer })
    })
    expect(screen.getByTestId("chat-attachment-chip-0")).toHaveTextContent("Folders aren't supported")
    expect(upload).not.toHaveBeenCalled()
  })

  it("stops the browser opening a file dropped outside the chat", async () => {
    await renderThread()
    const drop = new Event("drop", { bubbles: true, cancelable: true })
    Object.defineProperty(drop, "dataTransfer", { value: { types: ["Files"], files: [] } })
    window.dispatchEvent(drop)
    expect(drop.defaultPrevented).toBe(true)

    const textDrop = new Event("drop", { bubbles: true, cancelable: true })
    Object.defineProperty(textDrop, "dataTransfer", { value: { types: ["text/plain"], files: [] } })
    window.dispatchEvent(textDrop)
    expect(textDrop.defaultPrevented).toBe(false)
  })

  it("attaches a pasted screenshot", async () => {
    await renderThread()
    const file = screenshot("image.png")
    const paste = createEvent.paste(screen.getByTestId("chat-input"), {
      clipboardData: { files: [file], types: ["Files"], getData: () => "" },
    })
    await act(async () => {
      fireEvent(screen.getByTestId("chat-input"), paste)
    })
    expect(paste.defaultPrevented).toBe(true)
    expect(upload).toHaveBeenCalledWith(file, WS, expect.any(Function), expect.any(AbortSignal))
    await waitFor(() =>
      expect(screen.getByTestId("chat-attachment-chip-0")).toHaveAttribute("data-status", "ready"),
    )
  })

  it("pastes spreadsheet cells as text, not as a picture of them", async () => {
    await renderThread()
    const paste = createEvent.paste(screen.getByTestId("chat-input"), {
      clipboardData: {
        files: [screenshot("image.png")],
        types: ["text/plain", "text/html", "Files"],
        getData: (type: string) => (type === "text/plain" ? "a\tb\n1\t2" : ""),
      },
    })
    fireEvent(screen.getByTestId("chat-input"), paste)
    expect(paste.defaultPrevented).toBe(false)
    expect(upload).not.toHaveBeenCalled()
  })

  it("holds the send while a file uploads, and shows its progress", async () => {
    await renderThread()
    let finish: (value: UploadedAttachment) => void = () => {}
    let report: (fraction: number) => void = () => {}
    upload.mockImplementation((_file, _ws, onProgress) => {
      report = onProgress
      return new Promise((resolve) => {
        finish = resolve
      })
    })
    const file = screenshot()
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "Look at this" } })
    await act(async () => {
      fireEvent.drop(screen.getByTestId("chat-input"), fileDrag([file]))
    })

    act(() => report(0.4))
    expect(screen.getByTestId("chat-attachment-chip-0")).toHaveTextContent("Uploading 40%")
    expect(screen.getByTestId("chat-send")).toBeDisabled()
    fireEvent.keyDown(screen.getByTestId("chat-input"), { key: "Enter" })
    expect(screen.getByTestId("chat-input")).toHaveValue("Look at this")

    await act(async () => finish(uploaded(file)))
    expect(screen.getByTestId("chat-send")).not.toBeDisabled()
  })

  it("refuses an unsupported file and blocks the send until it is removed", async () => {
    await renderThread()
    await act(async () => {
      fireEvent.drop(
        screen.getByTestId("chat-thread-title"),
        fileDrag([new File(["MZ"], "setup.exe", { type: "application/x-msdownload" })]),
      )
    })
    const chip = screen.getByTestId("chat-attachment-chip-0")
    expect(chip).toHaveAttribute("data-status", "error")
    expect(chip).toHaveTextContent("Unsupported type")
    expect(upload).not.toHaveBeenCalled()

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } })
    expect(screen.getByTestId("chat-send")).toBeDisabled()
    fireEvent.click(screen.getByTestId("chat-attachment-remove-0"))
    expect(screen.queryByTestId("chat-attachment-chip-0")).toBeNull()
    expect(screen.getByTestId("chat-send")).not.toBeDisabled()
  })

  it("uploads a Markdown file the browser left untyped with its real type", async () => {
    await renderThread()
    await act(async () => {
      fireEvent.drop(screen.getByTestId("chat-thread-title"), fileDrag([new File(["# hi"], "notes.md")]))
    })
    const sent = upload.mock.calls[0][0]
    expect(sent.name).toBe("notes.md")
    expect(sent.type).toBe("text/markdown")
  })

  it("does not take a file named after an Object.prototype key for a text file", async () => {
    await renderThread()
    await act(async () => {
      fireEvent.drop(screen.getByTestId("chat-thread-title"), fileDrag([new File(["x"], "payload.constructor")]))
    })
    expect(screen.getByTestId("chat-attachment-chip-0")).toHaveAttribute("data-status", "error")
    expect(upload).not.toHaveBeenCalled()
  })

  it("takes an untyped image by its extension", async () => {
    await renderThread()
    await act(async () => {
      fireEvent.drop(screen.getByTestId("chat-thread-title"), fileDrag([new File(["x"], "shot.png")]))
    })
    expect(upload.mock.calls[0][0].type).toBe("image/png")
  })

  it("refuses a text file over 2 MB without uploading it", async () => {
    await renderThread()
    const big = new File([new Uint8Array(2 * 1024 * 1024 + 1)], "big.csv", { type: "text/csv" })
    await act(async () => {
      fireEvent.drop(screen.getByTestId("chat-thread-title"), fileDrag([big]))
    })
    await waitFor(() =>
      expect(screen.getByTestId("chat-attachment-chip-0")).toHaveAttribute("data-status", "error"),
    )
    expect(screen.getByTestId("chat-attachment-chip-0")).toHaveTextContent("up to 2 MB")
    expect(upload).not.toHaveBeenCalled()
  })

  it("shows the upload's error from the server", async () => {
    await renderThread()
    upload.mockRejectedValue(new Error("Images can be at most 5 MB."))
    await act(async () => {
      fireEvent.drop(screen.getByTestId("chat-thread-title"), fileDrag([screenshot()]))
    })
    await waitFor(() =>
      expect(screen.getByTestId("chat-attachment-chip-0")).toHaveTextContent("at most 5 MB"),
    )
  })

  it("takes at most 10 files per message", async () => {
    await renderThread()
    const files = Array.from({ length: 12 }, (_, i) => screenshot(`shot-${i}.png`))
    await act(async () => {
      fireEvent.drop(screen.getByTestId("chat-thread-title"), fileDrag(files))
    })
    expect(screen.getByTestId("chat-attachment-chip-9")).toBeInTheDocument()
    expect(screen.queryByTestId("chat-attachment-chip-10")).toBeNull()
    expect(screen.getByTestId("chat-attachment-notice")).toHaveTextContent("up to 10 files")
  })

  it("accepts a drop on the empty state of a new chat", async () => {
    mockApi([])
    render(<MemoryRouter><ChatPanel /></MemoryRouter>)
    const empty = await screen.findByTestId("chat-empty-state")
    fireEvent.dragEnter(empty, fileDrag([screenshot()]))
    expect(screen.getByTestId("chat-drop-overlay")).toBeInTheDocument()
    await act(async () => {
      fireEvent.drop(empty, fileDrag([screenshot()]))
    })
    await waitFor(() =>
      expect(screen.getByTestId("chat-attachment-chip-0")).toHaveAttribute("data-status", "ready"),
    )
  })
})

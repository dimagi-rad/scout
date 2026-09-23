import { StrictMode } from "react"
import { act, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { api, ApiError } from "@/api/client"
import type { TenantMembership } from "@/store/domainSlice"
import { useAppStore } from "@/store/store"
import { ChatCanvasPanel } from "./ChatCanvasPanel"
import type { CanvasCommitReport, CanvasProjection } from "./canvasApi"

vi.mock("@/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/api/client")>()
  return { ...original, api: { ...original.api, get: vi.fn(), post: vi.fn() } }
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function projection(thread: string, label: string, saved = false): CanvasProjection {
  return {
    canvas: {
      id: `canvas-${thread}`, thread_id: thread, status: "open",
      committed_at: saved ? "2026-09-15T02:00:00Z" : null,
      updated_at: saved ? "2026-09-15T02:00:00Z" : "2026-09-15T01:00:00Z",
    },
    objects: [{
      key: "dataset/shared_dataset", object_type: "dataset", object_uuid: "shared-dataset-uuid",
      change_type: "update", name: "shared_dataset", label, dataset: "",
      state: saved ? "unchanged" : "edited", summary: "", diff: {}, fields: {}, base: {},
    }],
    diagnostics: [], pending_count: saved ? 0 : 1, can_commit: !saved,
  }
}

function commitReport(next: CanvasProjection): CanvasCommitReport {
  return {
    committed: [{ object_uuid: "shared-dataset-uuid" }], blocked: false,
    blocking_diagnostics: [], conflicts: [], cube_schema: { ok: true }, projection: next,
  }
}

function panel(workspace: string, thread?: string) {
  return <MemoryRouter><ChatCanvasPanel workspaceId={workspace} threadId={thread} /></MemoryRouter>
}

function startMutation(kind: "apply" | "commit") {
  fireEvent.click(screen.getByTestId(
    kind === "apply" ? "canvas-revert-shared_dataset" : "canvas-commit-button",
  ))
}

let poll: () => void
beforeEach(() => {
  vi.mocked(api.get).mockReset()
  vi.mocked(api.post).mockReset()
  const originalSetInterval = window.setInterval.bind(window)
  vi.spyOn(window, "setInterval").mockImplementation((handler, delay, ...args) => {
    if (delay !== 15_000) {
      return originalSetInterval(handler, delay, ...args) as unknown as ReturnType<typeof window.setInterval>
    }
    poll = handler as () => void
    return 777 as unknown as ReturnType<typeof window.setInterval>
  })
})
afterEach(() => vi.restoreAllMocks())

describe("Canvas response context", () => {
  it.each(["same-workspace", "different-workspace"])("ignores A's delayed GET after B loads (%s)", async (scope) => {
    const old = deferred<CanvasProjection>()
    vi.mocked(api.get)
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce(projection("thread-b", "B current draft"))
    const view = render(panel("workspace-a", "thread-a"))
    view.rerender(panel(scope === "same-workspace" ? "workspace-a" : "workspace-b", "thread-b"))
    await screen.findByText("B current draft")
    await act(async () => old.resolve(projection("thread-a", "A stale draft")))
    expect(screen.queryByText("A stale draft")).not.toBeInTheDocument()
    expect(screen.getByText("B current draft")).toBeInTheDocument()
  })

  it.each(["success", "error"])("ignores the first A request after A → B → A (%s)", async (outcome) => {
    const old = deferred<CanvasProjection>()
    vi.mocked(api.get)
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce(projection("thread-b", "B draft"))
      .mockResolvedValueOnce(projection("thread-a", "Fresh A draft"))
    const view = render(panel("workspace-a", "thread-a"))
    view.rerender(panel("workspace-a", "thread-b"))
    await screen.findByText("B draft")
    view.rerender(panel("workspace-a", "thread-a"))
    await screen.findByText("Fresh A draft")
    await act(async () => {
      if (outcome === "success") old.resolve(projection("thread-a", "Old A draft"))
      else old.reject(new Error("Old A load failed"))
    })
    expect(screen.getByText("Fresh A draft")).toBeInTheDocument()
    expect(screen.queryByText("Old A draft")).not.toBeInTheDocument()
    expect(screen.queryByText("Old A load failed")).not.toBeInTheDocument()
  })

  it.each(["apply", "commit"] as const)("ignores a prior thread's %s success and finally while B is busy", async (kind) => {
    const old = deferred<CanvasProjection | CanvasCommitReport>()
    const current = deferred<CanvasProjection | CanvasCommitReport>()
    vi.mocked(api.get)
      .mockResolvedValueOnce(projection("thread-a", "A draft"))
      .mockResolvedValueOnce(projection("thread-b", "B draft"))
    vi.mocked(api.post).mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
    const view = render(panel("workspace-a", "thread-a"))
    await screen.findByText("A draft")
    startMutation(kind)
    view.rerender(panel("workspace-a", "thread-b"))
    await screen.findByText("B draft")
    expect(screen.getByTestId("canvas-commit-button")).toBeEnabled()
    startMutation(kind)
    await act(async () => old.resolve(
      kind === "apply" ? projection("thread-a", "A saved", true)
        : commitReport(projection("thread-a", "A saved", true)),
    ))
    expect(screen.getByText("B draft")).toBeInTheDocument()
    expect(screen.queryByText("A saved")).not.toBeInTheDocument()
    expect(screen.queryByText("Saved 1 change(s) to the semantic model.")).not.toBeInTheDocument()
    expect(screen.getByTestId("canvas-commit-button")).toBeDisabled()
    await act(async () => current.reject(new ApiError(400, "Current mutation failed")))
    expect(screen.getByTestId("canvas-error")).toHaveTextContent("Current mutation failed")
    expect(screen.getByTestId("canvas-commit-button")).toBeEnabled()
  })

  it.each(["apply", "commit"] as const)("ignores a prior thread's %s rejection while B's mutation is pending", async (kind) => {
    const old = deferred<CanvasProjection | CanvasCommitReport>()
    const current = deferred<CanvasProjection | CanvasCommitReport>()
    vi.mocked(api.get)
      .mockResolvedValueOnce(projection("thread-a", "A draft"))
      .mockResolvedValueOnce(projection("thread-b", "B draft"))
    vi.mocked(api.post).mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
    const view = render(panel("workspace-a", "thread-a"))
    await screen.findByText("A draft")
    startMutation(kind)
    view.rerender(panel("workspace-a", "thread-b"))
    await screen.findByText("B draft")
    startMutation(kind)
    await act(async () => old.reject(new ApiError(500, "Old mutation failed")))
    expect(screen.queryByTestId("canvas-error")).not.toBeInTheDocument()
    expect(screen.getByText("B draft")).toBeInTheDocument()
    expect(screen.getByTestId("canvas-commit-button")).toBeDisabled()
    await act(async () => current.resolve(
      kind === "apply" ? projection("thread-b", "B saved", true)
        : commitReport(projection("thread-b", "B saved", true)),
    ))
    expect(screen.getByText("B saved")).toBeInTheDocument()
  })

  it("does not leak an old load error into a no-conversation context", async () => {
    const old = deferred<CanvasProjection>()
    vi.mocked(api.get).mockReturnValueOnce(old.promise)
    const view = render(panel("workspace-a", "thread-a"))
    view.rerender(panel("workspace-a"))
    await act(async () => old.reject(new Error("Old load failed")))
    expect(screen.getByText("No conversation yet")).toBeInTheDocument()
    expect(screen.queryByText("Old load failed")).not.toBeInTheDocument()
  })

  it("keeps B's actual row attached to actions after A's stale load completes", async () => {
    const old = deferred<CanvasProjection>()
    vi.mocked(api.get)
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce(projection("thread-b", "B draft being reviewed"))
    vi.mocked(api.post).mockResolvedValueOnce(projection("thread-b", "B reverted", true))
    const view = render(panel("workspace-a", "thread-a"))
    view.rerender(panel("workspace-a", "thread-b"))
    await screen.findByText("B draft being reviewed")
    await act(async () => old.resolve(projection("thread-a", "A stale draft")))
    expect(screen.queryByText("A stale draft")).not.toBeInTheDocument()
    expect(screen.getByText("B draft being reviewed")).toBeInTheDocument()
    startMutation("apply")
    expect(api.post).toHaveBeenCalledWith(
      "/api/workspaces/workspace-a/threads/thread-b/canvas/apply/",
      { operations: [{ op: "revert_object", object: "dataset/shared-dataset-uuid" }] },
    )
    await screen.findByText("B reverted")
  })
})

describe("Canvas read and mutation ordering", () => {
  it.each(["commit", "remove"])("discards a poll that completes after %s", async (kind) => {
    const oldPoll = deferred<CanvasProjection>()
    vi.mocked(api.get)
      .mockResolvedValueOnce(projection("thread-a", "Old draft"))
      .mockReturnValueOnce(oldPoll.promise)
    vi.mocked(api.post).mockResolvedValueOnce(kind === "commit"
      ? commitReport(projection("thread-a", "Saved draft", true))
      : { ...projection("thread-a", "", true), objects: [] })
    render(panel("workspace-a", "thread-a"))
    await screen.findByText("Old draft")
    act(() => poll())
    fireEvent.click(screen.getByTestId(kind === "commit" ? "canvas-commit-button" : "canvas-remove-shared_dataset"))
    await screen.findByText(kind === "commit" ? "Saved draft" : "Nothing on the canvas")
    await act(async () => oldPoll.resolve(projection("thread-a", "Old draft")))
    expect(screen.queryByText("Old draft")).not.toBeInTheDocument()
    expect(screen.getByTestId("canvas-pending-count")).toHaveTextContent("No pending changes")
    expect(screen.getByTestId("canvas-commit-button")).toBeDisabled()
  })

  it("lets a manual refresh supersede a slow poll, including the poll's finally", async () => {
    const oldPoll = deferred<CanvasProjection>()
    const refresh = deferred<CanvasProjection>()
    vi.mocked(api.get)
      .mockResolvedValueOnce(projection("thread-a", "Initial draft"))
      .mockReturnValueOnce(oldPoll.promise)
      .mockReturnValueOnce(refresh.promise)
    render(panel("workspace-a", "thread-a"))
    await screen.findByText("Initial draft")
    act(() => poll())
    fireEvent.click(screen.getByTestId("canvas-refresh-button"))
    await act(async () => oldPoll.resolve(projection("thread-a", "Old poll")))
    act(() => poll())
    expect(api.get).toHaveBeenCalledTimes(3)
    expect(screen.getByTestId("canvas-refresh-button")).toBeDisabled()
    await act(async () => refresh.resolve(projection("thread-a", "Latest refresh")))
    expect(screen.getByText("Latest refresh")).toBeInTheDocument()
    expect(screen.queryByText("Old poll")).not.toBeInTheDocument()
    expect(screen.getByTestId("canvas-refresh-button")).toBeEnabled()
  })

  it("does not let polling supersede an initial load or leave loading stuck", async () => {
    const initial = deferred<CanvasProjection>()
    vi.mocked(api.get).mockReturnValueOnce(initial.promise)
    render(panel("workspace-a", "thread-a"))
    act(() => { poll(); poll() })
    expect(api.get).toHaveBeenCalledTimes(1)
    await act(async () => initial.reject(new Error("Initial load failed")))
    expect(screen.getByText("Canvas unavailable")).toBeInTheDocument()
    expect(screen.getByText("Initial load failed")).toBeInTheDocument()
    vi.mocked(api.get).mockResolvedValueOnce(projection("thread-a", "Retry recovered"))
    fireEvent.click(screen.getByRole("button", { name: "Try Again" }))
    await screen.findByText("Retry recovered")
  })

  it.each(["apply", "commit"] as const)("prevents duplicate %s submissions, pauses reads, and resumes after success", async (kind) => {
    const mutation = deferred<CanvasProjection | CanvasCommitReport>()
    vi.mocked(api.get).mockResolvedValueOnce(projection("thread-a", "Draft"))
    vi.mocked(api.post).mockReturnValueOnce(mutation.promise)
    render(panel("workspace-a", "thread-a"))
    await screen.findByText("Draft")
    act(() => { startMutation(kind); startMutation(kind) })
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId("canvas-refresh-button")).toBeDisabled()
    expect(screen.getByTestId("canvas-revert-shared_dataset")).toBeDisabled()
    act(() => poll())
    fireEvent.click(screen.getByTestId("canvas-refresh-button"))
    expect(api.get).toHaveBeenCalledTimes(1)
    await act(async () => mutation.resolve(kind === "apply"
      ? projection("thread-a", "Saved draft", true)
      : commitReport(projection("thread-a", "Saved draft", true))))
    expect(screen.getByText("Saved draft")).toBeInTheDocument()
    vi.mocked(api.get).mockResolvedValueOnce(projection("thread-a", "Agent's next edit"))
    act(() => poll())
    await screen.findByText("Agent's next edit")
    expect(screen.getByTestId("canvas-commit-button")).toBeEnabled()
  })

  it.each(["apply", "commit"] as const)("unblocks reads and retry after current %s fails", async (kind) => {
    vi.mocked(api.get).mockResolvedValueOnce(projection("thread-a", "Draft"))
    vi.mocked(api.post).mockRejectedValueOnce(new ApiError(400, "Please retry this change"))
    render(panel("workspace-a", "thread-a"))
    await screen.findByText("Draft")
    startMutation(kind)
    await screen.findByText("Please retry this change")
    expect(screen.getByTestId("canvas-refresh-button")).toBeEnabled()
    vi.mocked(api.get).mockResolvedValueOnce(projection("thread-a", "Still pending"))
    act(() => poll())
    await screen.findByText("Still pending")
    vi.mocked(api.post).mockResolvedValueOnce(kind === "apply"
      ? projection("thread-a", "Retried successfully", true)
      : commitReport(projection("thread-a", "Retried successfully", true)))
    startMutation(kind)
    await screen.findByText("Retried successfully")
    expect(api.post).toHaveBeenCalledTimes(2)
  })

  it("keeps StrictMode's retired initial load from replacing the live load", async () => {
    const old = deferred<CanvasProjection>()
    vi.mocked(api.get)
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce(projection("thread-a", "Live strict-mode draft"))
    render(<StrictMode>{panel("workspace-a", "thread-a")}</StrictMode>)
    await screen.findByText("Live strict-mode draft")
    await act(async () => old.resolve(projection("thread-a", "Retired strict-mode draft")))
    expect(screen.queryByText("Retired strict-mode draft")).not.toBeInTheDocument()
    expect(screen.getByText("Live strict-mode draft")).toBeInTheDocument()
  })

  it("makes a captured poll inert after unmount", async () => {
    vi.mocked(api.get).mockResolvedValueOnce(projection("thread-a", "Draft"))
    const view = render(panel("workspace-a", "thread-a"))
    await screen.findByText("Draft")
    const retiredPoll = poll
    view.unmount()
    act(() => retiredPoll())
    expect(api.get).toHaveBeenCalledTimes(1)
  })
})

describe("Canvas for read-only members", () => {
  afterEach(() => useAppStore.setState({ domains: [] }))

  it("replaces Save all with a read-only hint", async () => {
    useAppStore.setState({
      domains: [{ id: "workspace-a", role: "read" } as TenantMembership],
    })
    vi.mocked(api.get).mockResolvedValueOnce(projection("thread-a", "A draft"))
    render(panel("workspace-a", "thread-a"))

    await screen.findByText("A draft")
    expect(screen.queryByTestId("canvas-commit-button")).not.toBeInTheDocument()
    expect(screen.getByTestId("canvas-readonly-hint")).toHaveTextContent("Read-only access")
  })
})

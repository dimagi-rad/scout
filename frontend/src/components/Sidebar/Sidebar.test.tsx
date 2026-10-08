import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { Thread } from "@/store/uiSlice"
import { RUNNING_THREADS_POLL_MS, Sidebar } from "./Sidebar"

const mocks = vi.hoisted(() => {
  const fetchDomains = vi.fn()
  const revalidateDomains = vi.fn(() => Promise.resolve("fetched"))
  const fetchThreads = vi.fn()
  const logout = vi.fn()
  const newThread = vi.fn()
  const selectThread = vi.fn()
  const retryAccessVerification = vi.fn(() => Promise.resolve())

  return {
    state: {
      user: { id: "user-1" },
      activeDomainId: "workspace-1",
      domains: [{ id: "workspace-1", name: "Test Workspace" }],
      threadId: null,
      threads: [] as Thread[],
      threadsStatus: "loaded",
      threadsAccessDenialReason: null as string | null,
      threadsAccessRetryable: false,
      localTurnThreadIds: new Set<string>(),
      domainActions: { fetchDomains, revalidateDomains },
      authActions: { logout },
      uiActions: { fetchThreads, newThread, selectThread, retryAccessVerification },
    },
    fetchDomains,
    revalidateDomains,
    fetchThreads,
    logout,
    newThread,
    selectThread,
    retryAccessVerification,
  }
})

vi.mock("@/store/store", () => ({
  useAppStore: (selector: (state: typeof mocks.state) => unknown) => selector(mocks.state),
}))

vi.mock("@/contexts/WorkspaceJobsContext", () => ({
  useWorkspaceJobs: () => ({
    jobsByThreadId: {},
    recentlyCompletedThreadIds: [],
  }),
}))

function renderSidebar() {
  return render(
    <MemoryRouter initialEntries={["/artifacts"]}>
      <Sidebar />
    </MemoryRouter>
  )
}

describe("Sidebar hover behavior", () => {
  let originalHasFocus: typeof document.hasFocus
  let originalMatches: typeof Element.prototype.matches
  let originalRequestAnimationFrame: typeof window.requestAnimationFrame

  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.threadId = null
    mocks.state.threads = []
    mocks.state.threadsStatus = "loaded"
    originalHasFocus = document.hasFocus
    originalMatches = Element.prototype.matches
    originalRequestAnimationFrame = window.requestAnimationFrame

    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    })

    Object.defineProperty(document, "hasFocus", {
      configurable: true,
      value: undefined,
    })

    window.requestAnimationFrame = (callback: FrameRequestCallback) => {
      callback(0)
      return 1
    }
  })

  afterEach(() => {
    Object.defineProperty(document, "hasFocus", {
      configurable: true,
      value: originalHasFocus,
    })
    Element.prototype.matches = originalMatches
    window.requestAnimationFrame = originalRequestAnimationFrame
  })

  it("expands on pointer enter even when document.hasFocus is unavailable", async () => {
    renderSidebar()

    const shell = screen.getByTestId("sidebar-shell")
    Element.prototype.matches = function matches(selector: string) {
      if (selector === ":hover" && this === shell) return true
      return originalMatches.call(this, selector)
    }

    fireEvent.pointerEnter(shell)

    await waitFor(() => {
      expect(shell).toHaveAttribute("data-expanded", "true")
    })
  })

  it("stays expanded after navigation while the sidebar is still hovered", async () => {
    renderSidebar()

    const shell = screen.getByTestId("sidebar-shell")
    Element.prototype.matches = function matches(selector: string) {
      if (selector === ":hover" && this === shell) return true
      return originalMatches.call(this, selector)
    }

    fireEvent.pointerEnter(shell)
    fireEvent.click(screen.getByRole("link", { name: "Datasets" }))

    await waitFor(() => {
      expect(shell).toHaveAttribute("data-expanded", "true")
    })
  })

  it("shows each thread's one title, falling back to Untitled", () => {
    mocks.state.threads = [
      {
        id: "thread-preview",
        title: "Build an artifact from example queries",
        title_is_custom: false,
        title_source: "first_message",
        created_at: "2026-07-01T12:00:00Z",
        updated_at: "2026-07-01T12:00:00Z",
        last_viewed_at: null,
      },
      {
        id: "thread-title",
        title: "Quarterly review",
        title_is_custom: true,
        title_source: "user",
        created_at: "2026-07-01T12:00:00Z",
        updated_at: "2026-07-01T12:00:00Z",
        last_viewed_at: null,
      },
      {
        id: "thread-blank",
        title: "",
        title_is_custom: false,
        title_source: "first_message",
        created_at: "2026-07-01T12:00:00Z",
        updated_at: "2026-07-01T12:00:00Z",
        last_viewed_at: null,
      },
    ]

    renderSidebar()

    expect(screen.getByTestId("sidebar-thread-thread-preview")).toHaveTextContent(
      "Build an artifact from example queries",
    )
    expect(screen.getByTestId("sidebar-thread-thread-title")).toHaveTextContent(
      "Quarterly review",
    )
    expect(screen.getByTestId("sidebar-thread-thread-blank")).toHaveTextContent("Untitled")
  })
})

describe("Sidebar workspace revalidation (#355)", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers({ toFake: ["Date"] })
    vi.setSystemTime(new Date("2026-09-29T12:00:00Z"))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it("revalidates silently when the tab becomes visible again, at most every 15s", () => {
    renderSidebar()
    expect(mocks.fetchDomains).toHaveBeenCalledOnce()
    expect(mocks.revalidateDomains).not.toHaveBeenCalled()

    fireEvent(document, new Event("visibilitychange"))
    expect(mocks.revalidateDomains).toHaveBeenCalledOnce()
    // Never joins a request from before the return, which can't include a new grant.
    expect(mocks.revalidateDomains).toHaveBeenCalledWith({ fresh: true })

    // A tab switch also focuses the window; one refetch covers both.
    fireEvent.focus(window)
    expect(mocks.revalidateDomains).toHaveBeenCalledOnce()

    vi.setSystemTime(new Date("2026-09-29T12:00:16Z"))
    fireEvent.focus(window)
    expect(mocks.revalidateDomains).toHaveBeenCalledTimes(2)
    // The mount-time full fetch is the only one that may show loading state.
    expect(mocks.fetchDomains).toHaveBeenCalledOnce()
  })

  it("doesn't spend the throttle window on a revalidation that was skipped (D5)", async () => {
    mocks.revalidateDomains.mockResolvedValueOnce("skipped")
    renderSidebar()

    fireEvent(document, new Event("visibilitychange"))
    await waitFor(() => expect(mocks.revalidateDomains).toHaveBeenCalledOnce())
    await Promise.resolve()

    vi.setSystemTime(new Date("2026-09-29T12:00:05Z"))
    fireEvent.focus(window)
    expect(mocks.revalidateDomains).toHaveBeenCalledTimes(2)
  })

  it("polls every minute while you stay on the tab, and not while it's hidden", () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] })
    vi.setSystemTime(new Date("2026-09-29T12:00:00Z"))
    const { unmount } = renderSidebar()

    vi.advanceTimersByTime(59_000)
    expect(mocks.revalidateDomains).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1_000)
    expect(mocks.revalidateDomains).toHaveBeenCalledOnce()
    // A tick has no moment to be fresh for, so it may join a request already in flight.
    expect(mocks.revalidateDomains).toHaveBeenCalledWith({ fresh: false })

    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" })
    try {
      vi.advanceTimersByTime(60_000)
      expect(mocks.revalidateDomains).toHaveBeenCalledOnce()
    } finally {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" })
    }

    unmount()
    vi.advanceTimersByTime(60_000)
    expect(mocks.revalidateDomains).toHaveBeenCalledOnce()
  })

  it("does not revalidate while the tab is hidden", () => {
    renderSidebar()
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" })
    try {
      fireEvent(document, new Event("visibilitychange"))
      expect(mocks.revalidateDomains).not.toHaveBeenCalled()
    } finally {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" })
    }
  })
})

describe("Sidebar access denial", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.state.threadsStatus = "error"
  })

  afterEach(() => {
    mocks.state.threadsStatus = "loaded"
    mocks.state.threadsAccessDenialReason = null
    mocks.state.threadsAccessRetryable = false
  })

  it("summarises a lost source in one line and offers both actions as buttons", () => {
    mocks.state.threadsAccessDenialReason = "tenant_access_lost"
    mocks.state.threadsAccessRetryable = true
    renderSidebar()

    expect(screen.getByTestId("sidebar-threads-access-lost")).toHaveTextContent(
      "You don't have access to all of this workspace's sources.",
    )
    fireEvent.click(screen.getByRole("button", { name: "Retry verification" }))
    expect(mocks.retryAccessVerification).toHaveBeenCalledWith("workspace-1")
    const connections = screen.getByTestId("sidebar-threads-connected-accounts")
    expect(connections).toHaveAttribute("href", "/settings/connections")
    expect(connections).toHaveAttribute("data-slot", "button")
  })

  it("offers no verification retry when only reconnecting helps", () => {
    mocks.state.threadsAccessDenialReason = "credential_expired"
    renderSidebar()

    expect(screen.getByTestId("sidebar-threads-access-lost")).toHaveTextContent(
      "Your sign-in for one of this workspace's sources expired.",
    )
    expect(screen.queryByTestId("sidebar-threads-retry-verification")).toBeNull()
    expect(screen.getByTestId("sidebar-threads-connected-accounts")).toBeInTheDocument()
  })

  it("does not point a workspace without sources at Connected Accounts", () => {
    mocks.state.threadsAccessDenialReason = "no_sources"
    renderSidebar()

    expect(screen.getByTestId("sidebar-threads-access-lost")).toHaveTextContent(
      "This workspace has no data sources.",
    )
    expect(screen.queryByTestId("sidebar-threads-connected-accounts")).toBeNull()
  })
})

describe("Sidebar agent model label", () => {
  afterEach(() => {
    mocks.state.user = { id: "user-1" }
  })

  it("shows the friendly model name beside the wordmark", () => {
    mocks.state.user = {
      id: "user-1",
      agent_model: { id: "claude-opus-5-5", label: "Opus 5.5" },
    } as typeof mocks.state.user
    renderSidebar()

    const label = screen.getByTestId("app-model-label")
    expect(label).toHaveTextContent("Opus 5.5")
    expect(label).toHaveClass("scout-sidebar-label")
  })

  it("renders nothing when the server sends no model", () => {
    renderSidebar()

    expect(screen.queryByTestId("app-model-label")).toBeNull()
  })
})

describe("Sidebar running threads (#856)", () => {
  const running = (turn_running: boolean): Thread => ({
    id: "thread-running",
    title: "Visits by district",
    title_is_custom: false,
    title_source: "generated",
    created_at: "2026-07-01T12:00:00Z",
    updated_at: "2026-07-01T12:00:00Z",
    last_viewed_at: "2026-07-01T12:00:00Z",
    turn_running,
  })

  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] })
    mocks.state.threadId = null
  })

  afterEach(() => {
    vi.useRealTimers()
    mocks.state.threads = []
    mocks.fetchThreads.mockReset()
  })

  it("shows a spinner while a thread's turn runs, refetching until it clears", async () => {
    mocks.state.threads = [running(true)]
    const { rerender } = renderSidebar()
    expect(screen.getByTestId("sidebar-thread-running-thread-running")).toBeInTheDocument()
    const mountFetches = mocks.fetchThreads.mock.calls.length

    // Still running at the first refetch: the next one waits longer.
    await vi.advanceTimersByTimeAsync(RUNNING_THREADS_POLL_MS)
    expect(mocks.fetchThreads).toHaveBeenCalledTimes(mountFetches + 1)
    await vi.advanceTimersByTimeAsync(RUNNING_THREADS_POLL_MS)
    expect(mocks.fetchThreads).toHaveBeenCalledTimes(mountFetches + 1)
    mocks.fetchThreads.mockImplementation(() => {
      mocks.state.threads = [running(false)]
    })
    await vi.advanceTimersByTimeAsync(RUNNING_THREADS_POLL_MS * 0.5)
    expect(mocks.fetchThreads).toHaveBeenCalledTimes(mountFetches + 2)

    rerender(
      <MemoryRouter initialEntries={["/artifacts"]}>
        <Sidebar />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId("sidebar-thread-running-thread-running")).toBeNull()
    await vi.advanceTimersByTimeAsync(RUNNING_THREADS_POLL_MS * 10)
    expect(mocks.fetchThreads).toHaveBeenCalledTimes(mountFetches + 2)
  })

  it("shows this tab's own turn running without polling for it", async () => {
    mocks.state.threads = [running(false)]
    mocks.state.localTurnThreadIds = new Set(["thread-running"])
    try {
      renderSidebar()
      const mountFetches = mocks.fetchThreads.mock.calls.length
      expect(screen.getByTestId("sidebar-thread-running-thread-running")).toBeInTheDocument()

      // The list may say running too (refetched mid-turn): still this tab's to end.
      mocks.state.threads = [running(true)]
      await vi.advanceTimersByTimeAsync(RUNNING_THREADS_POLL_MS * 4)
      expect(mocks.fetchThreads).toHaveBeenCalledTimes(mountFetches)
    } finally {
      mocks.state.localTurnThreadIds = new Set()
    }
  })

  it("does not refetch while the tab is hidden", async () => {
    mocks.state.threads = [running(true)]
    renderSidebar()
    const mountFetches = mocks.fetchThreads.mock.calls.length
    Object.defineProperty(document, "hidden", { configurable: true, value: true })
    try {
      await vi.advanceTimersByTimeAsync(RUNNING_THREADS_POLL_MS * 4)
      expect(mocks.fetchThreads).toHaveBeenCalledTimes(mountFetches)
    } finally {
      Object.defineProperty(document, "hidden", { configurable: true, value: false })
    }
  })

  it("does not poll while the list is denied", async () => {
    mocks.state.threads = [running(true)]
    mocks.state.threadsStatus = "error"
    mocks.state.threadsAccessDenialReason = "tenant_access_lost"
    try {
      renderSidebar()
      const mountFetches = mocks.fetchThreads.mock.calls.length
      await vi.advanceTimersByTimeAsync(RUNNING_THREADS_POLL_MS * 4)
      expect(mocks.fetchThreads).toHaveBeenCalledTimes(mountFetches)
    } finally {
      mocks.state.threadsStatus = "loaded"
      mocks.state.threadsAccessDenialReason = null
    }
  })

  it("does not poll when no listed turn is running", async () => {
    mocks.state.threads = [running(false)]
    renderSidebar()
    const mountFetches = mocks.fetchThreads.mock.calls.length

    await vi.advanceTimersByTimeAsync(RUNNING_THREADS_POLL_MS * 4)

    expect(mocks.fetchThreads).toHaveBeenCalledTimes(mountFetches)
    expect(screen.queryByTestId("sidebar-thread-running-thread-running")).toBeNull()
  })

  it("stops polling on unmount", async () => {
    mocks.state.threads = [running(true)]
    const { unmount } = renderSidebar()
    const mountFetches = mocks.fetchThreads.mock.calls.length
    unmount()

    await vi.advanceTimersByTimeAsync(RUNNING_THREADS_POLL_MS * 4)

    expect(mocks.fetchThreads).toHaveBeenCalledTimes(mountFetches)
  })
})

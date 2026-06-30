import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { Sidebar } from "./Sidebar"

const mocks = vi.hoisted(() => {
  const fetchDomains = vi.fn()
  const fetchThreads = vi.fn()
  const logout = vi.fn()
  const newThread = vi.fn()
  const selectThread = vi.fn()

  return {
    state: {
      user: { id: "user-1" },
      activeDomainId: "workspace-1",
      domains: [{ id: "workspace-1", name: "Test Workspace" }],
      threadId: null,
      threads: [],
      threadsStatus: "success",
      domainActions: { fetchDomains },
      authActions: { logout },
      uiActions: { fetchThreads, newThread, selectThread },
    },
    fetchDomains,
    fetchThreads,
    logout,
    newThread,
    selectThread,
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
})

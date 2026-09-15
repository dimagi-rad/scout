import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, render, screen, waitFor } from "@testing-library/react"
import { createMemoryRouter, MemoryRouter, Route, RouterProvider, Routes, useLocation } from "react-router-dom"
import { api } from "@/api/client"
import { useAppStore } from "@/store/store"
import { useWorkspaceThreadSync } from "@/hooks/useWorkspaceThreadSync"
import { getRecentWorkspaceIds } from "@/lib/recentWorkspaces"
import type { TenantMembership } from "@/store/domainSlice"

// Workspace ids with EMPTY names so workspacePath yields the bare
// `/workspaces/<id>` form (no slug) and URLs are fully predictable.
const WS_A = "11111111-1111-1111-1111-111111111111"
const WS_B = "22222222-2222-2222-2222-222222222222"
const THREAD_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const THREAD_STALE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

function domain(id: string, name = ""): TenantMembership {
  return {
    id,
    name,
    display_name: name,
    is_auto_created: false,
    role: "manage",
    tenants: [],
    member_count: 1,
    schema_status: "available",
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
  }
}

function Probe({ pathPrefix = "" }: { pathPrefix?: string }) {
  useWorkspaceThreadSync(pathPrefix)
  const loc = useLocation()
  return <div data-testid="path">{loc.pathname}</div>
}

beforeEach(() => {
  vi.spyOn(api, "get").mockResolvedValue([])
  vi.spyOn(api, "post").mockResolvedValue(undefined)
})

afterEach(() => vi.restoreAllMocks())

function renderPrettyChat(initialPath: string, pathPrefix = "") {
  const router = createMemoryRouter(
    [
      "/workspaces/:workspaceId/chat",
      "/workspaces/:workspaceId/chat/:threadId",
      "/workspaces/:slug/:workspaceId/chat",
      "/workspaces/:slug/:workspaceId/chat/:threadId",
    ].map((path) => ({
      path: `${pathPrefix}${path}`,
      element: <Probe pathPrefix={pathPrefix} />,
    })),
    { initialEntries: [initialPath] },
  )
  render(<RouterProvider router={router} />)
  return router
}

describe("useWorkspaceThreadSync — no cross-workspace thread carry (00c423d)", () => {
  beforeEach(() => {
    localStorage.clear()
    useAppStore.setState({
      domains: [domain(WS_A), domain(WS_B)],
      domainsStatus: "loaded",
      activeDomainId: WS_A,
      threadId: THREAD_A,
    })
  })

  it("navigates to the new workspace with a fresh thread, never grafting the old one", async () => {
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WS_A}/chat/${THREAD_A}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/chat/:threadId" element={<Probe />} />
          <Route path="/workspaces/:workspaceId/chat" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    )

    // URL → store reconciled; address bar stays on A/threadA.
    await waitFor(() =>
      expect(screen.getByTestId("path").textContent).toBe(
        `/workspaces/${WS_A}/chat/${THREAD_A}`,
      ),
    )

    act(() => {
      useAppStore.getState().domainActions.setActiveDomain(WS_B)
    })

    await waitFor(() => {
      const path = screen.getByTestId("path").textContent ?? ""
      expect(path.startsWith(`/workspaces/${WS_B}/chat/`)).toBe(true)
      expect(path).not.toContain(THREAD_A)
    })
  })

  it("keeps an explicit deep-linked thread when the store starts on a different thread", async () => {
    useAppStore.setState({
      domains: [domain(WS_A), domain(WS_B)],
      domainsStatus: "loaded",
      activeDomainId: WS_A,
      threadId: THREAD_STALE,
    })

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WS_A}/chat/${THREAD_A}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/chat/:threadId" element={<Probe />} />
          <Route path="/workspaces/:workspaceId/chat" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    )

    await waitFor(() => {
      expect(screen.getByTestId("path").textContent).toBe(
        `/workspaces/${WS_A}/chat/${THREAD_A}`,
      )
      expect(useAppStore.getState().threadId).toBe(THREAD_A)
    })
  })

  it("records an opened workspace even when it is already active", async () => {
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WS_A}/chat/${THREAD_A}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/chat/:threadId" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    )

    await waitFor(() => expect(getRecentWorkspaceIds()).toEqual([WS_A]))
  })
})

describe("useWorkspaceThreadSync — thread identity during slug canonicalization", () => {
  beforeEach(() => {
    localStorage.clear()
    useAppStore.setState({
      domains: [domain(WS_A, "Workspace A"), domain(WS_B, "Workspace B")],
      domainsStatus: "loaded",
      activeDomainId: WS_A,
      threadId: THREAD_A,
    })
  })

  it.each(["", "/embed"])("adds the current thread while canonicalizing a bare %s chat URL", async (prefix) => {
    renderPrettyChat(`${prefix}/workspaces/${WS_A}/chat`, prefix)

    await waitFor(() => expect(screen.getByTestId("path")).toHaveTextContent(
      `${prefix}/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
    ))
    expect(useAppStore.getState().threadId).toBe(THREAD_A)
  })

  it("adds a thread to an already-pretty URL", async () => {
    renderPrettyChat(`/workspaces/workspace-a/${WS_A}/chat`)

    await waitFor(() => expect(screen.getByTestId("path")).toHaveTextContent(
      `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
    ))
  })

  it("adopts a different URL workspace without keeping its previous thread", async () => {
    renderPrettyChat(`/workspaces/${WS_B}/chat`)

    await waitFor(() => {
      const { activeDomainId, threadId } = useAppStore.getState()
      expect(activeDomainId).toBe(WS_B)
      expect(threadId).not.toBe(THREAD_A)
      expect(screen.getByTestId("path")).toHaveTextContent(
        `/workspaces/workspace-b/${WS_B}/chat/${threadId}`,
      )
    })
  })

  it("retains the URL thread while correcting a stale slug and workspace", async () => {
    renderPrettyChat(`/workspaces/old-name/${WS_B}/chat/${THREAD_STALE}`)

    await waitFor(() => {
      expect(useAppStore.getState().activeDomainId).toBe(WS_B)
      expect(useAppStore.getState().threadId).toBe(THREAD_STALE)
      expect(screen.getByTestId("path")).toHaveTextContent(
        `/workspaces/workspace-b/${WS_B}/chat/${THREAD_STALE}`,
      )
    })
  })

  it("retains the thread when workspace names load after the route", async () => {
    useAppStore.setState({ domains: [], domainsStatus: "loading" })
    renderPrettyChat(`/workspaces/${WS_A}/chat`)
    await waitFor(() => expect(screen.getByTestId("path")).toHaveTextContent(
      `/workspaces/${WS_A}/chat/${THREAD_A}`,
    ))

    act(() => {
      useAppStore.setState({ domains: [domain(WS_A, "Workspace A")], domainsStatus: "loaded" })
    })
    await waitFor(() => expect(screen.getByTestId("path")).toHaveTextContent(
      `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
    ))
  })

  it("keeps new-thread navigation and back/forward in sync after canonicalization", async () => {
    const router = renderPrettyChat(`/workspaces/${WS_A}/chat`)
    await waitFor(() => expect(screen.getByTestId("path")).toHaveTextContent(
      `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
    ))

    act(() => useAppStore.getState().uiActions.newThread())
    const freshThread = useAppStore.getState().threadId
    await waitFor(() => expect(screen.getByTestId("path")).toHaveTextContent(
      `/workspaces/workspace-a/${WS_A}/chat/${freshThread}`,
    ))
    await act(() => router.navigate(-1))
    expect(useAppStore.getState().threadId).toBe(THREAD_A)
    expect(screen.getByTestId("path")).toHaveTextContent(
      `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
    )
    await act(() => router.navigate(1))
    expect(useAppStore.getState().threadId).toBe(freshThread)
    expect(screen.getByTestId("path")).toHaveTextContent(
      `/workspaces/workspace-a/${WS_A}/chat/${freshThread}`,
    )
  })
})

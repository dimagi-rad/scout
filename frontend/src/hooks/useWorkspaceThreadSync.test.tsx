import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { createMemoryRouter, MemoryRouter, Route, RouterProvider, Routes, useLocation } from "react-router-dom"
import { api } from "@/api/client"
import { workspaceApi } from "@/api/workspaces"
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
    // Select the workspace before seeding its thread: workspace changes reset thread state.
    useAppStore.setState({ activeDomainId: WS_A })
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

  it("preserves a deep-linked thread while changing workspaces", async () => {
    useAppStore.setState({ activeDomainId: WS_B })
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WS_A}/chat/${THREAD_A}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/chat/:threadId" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(useAppStore.getState().activeDomainId).toBe(WS_A)
      expect(useAppStore.getState().threadId).toBe(THREAD_A)
      expect(screen.getByTestId("path").textContent).toBe(`/workspaces/${WS_A}/chat/${THREAD_A}`)
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
    // Select the workspace before seeding its thread: workspace changes reset thread state.
    useAppStore.setState({ activeDomainId: WS_A })
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

  it.each(["", "/embed"])("synchronizes a cold %s entry before and after deferred workspace loading", async (prefix) => {
    useAppStore.setState({
      domains: [], domainsStatus: "idle", activeDomainId: null, threadId: crypto.randomUUID(),
    })
    let finishLoading!: (domains: TenantMembership[]) => void
    vi.spyOn(workspaceApi, "list").mockReturnValue(new Promise((resolve) => {
      finishLoading = resolve
    }))
    let loading!: Promise<void>
    act(() => { loading = useAppStore.getState().domainActions.fetchDomains() })
    renderPrettyChat(`${prefix}/workspaces/${WS_A}/chat`, prefix)

    const freshThread = useAppStore.getState().threadId
    expect(useAppStore.getState().activeDomainId).toBe(WS_A)
    await act(async () => {
      finishLoading([domain(WS_B, "Workspace B"), domain(WS_A, "Workspace A")])
      await loading
    })
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe(
      `${prefix}/workspaces/workspace-a/${WS_A}/chat/${freshThread}`,
    ))
    expect(useAppStore.getState().threadId).toBe(freshThread)
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

  it("adds a fresh thread when navigation adopts a different bare workspace URL", async () => {
    const router = renderPrettyChat(`/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`)
    await act(() => router.navigate(`/workspaces/${WS_B}/chat`))
    const freshThread = useAppStore.getState().threadId
    expect(freshThread).not.toBe(THREAD_A)
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe(
      `/workspaces/workspace-b/${WS_B}/chat/${freshThread}`,
    ))
    await act(() => router.navigate(-1))
    expect(screen.getByTestId("path").textContent).toBe(
      `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
    )
    expect(useAppStore.getState().threadId).toBe(THREAD_A)
  })

  it("rechecks the list before dropping a deep link to a workspace it doesn't know (#355)", async () => {
    let finishRecheck!: (domains: TenantMembership[]) => void
    const list = vi.spyOn(workspaceApi, "list").mockReturnValue(new Promise((resolve) => {
      finishRecheck = resolve
    }))
    const WS_NEW = "33333333-3333-3333-3333-333333333333"
    renderPrettyChat(`/workspaces/${WS_NEW}/chat/${THREAD_STALE}`)

    await waitFor(() => expect(list).toHaveBeenCalledOnce())
    // Still on the link while the recheck is in flight, not bounced to the default.
    expect(screen.getByTestId("path").textContent).toBe(`/workspaces/${WS_NEW}/chat/${THREAD_STALE}`)
    expect(useAppStore.getState().domainsStatus).toBe("loaded")

    await act(async () => {
      finishRecheck([domain(WS_NEW, "Just Added"), domain(WS_A, "Workspace A"), domain(WS_B, "Workspace B")])
    })
    await waitFor(() => {
      expect(useAppStore.getState().activeDomainId).toBe(WS_NEW)
      expect(useAppStore.getState().threadId).toBe(THREAD_STALE)
      expect(screen.getByTestId("path").textContent).toBe(
        `/workspaces/just-added/${WS_NEW}/chat/${THREAD_STALE}`,
      )
    })
  })

  it("stops holding the link after 5s when the recheck stalls", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let release!: (domains: TenantMembership[]) => void
    try {
      vi.spyOn(workspaceApi, "list").mockReturnValue(new Promise((resolve) => { release = resolve }))
      const WS_SLOW = "55555555-5555-5555-5555-555555555555"
      renderPrettyChat(`/workspaces/${WS_SLOW}/chat`)
      await waitFor(() => expect(workspaceApi.list).toHaveBeenCalledOnce())
      expect(screen.getByTestId("path").textContent).toBe(`/workspaces/${WS_SLOW}/chat`)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000)
      })
      await waitFor(() => expect(screen.getByTestId("path").textContent).toBe(
        `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
      ))
    } finally {
      vi.useRealTimers()
      // Settle the slice's shared in-flight request so later tests start clean.
      await act(async () => release(useAppStore.getState().domains))
    }
  })

  it("falls back to the active workspace when the recheck doesn't find the link's workspace", async () => {
    vi.spyOn(workspaceApi, "list").mockResolvedValue([
      domain(WS_A, "Workspace A"), domain(WS_B, "Workspace B"),
    ])
    const WS_GONE = "44444444-4444-4444-4444-444444444444"
    renderPrettyChat(`/workspaces/${WS_GONE}/chat`)

    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe(
      `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
    ))
    expect(useAppStore.getState().activeDomainId).toBe(WS_A)
  })

  it("rechecks with a new request, not one that started before the link opened (D1)", async () => {
    const WS_NEW = "33333333-3333-3333-3333-333333333333"
    let finishOlder!: (domains: TenantMembership[]) => void
    const list = vi.spyOn(workspaceApi, "list")
      .mockReturnValueOnce(new Promise((resolve) => { finishOlder = resolve }))
      .mockResolvedValueOnce([domain(WS_NEW, "Just Added"), domain(WS_A, "Workspace A")])
    // A focus refresh already in flight when the link is opened, from before the grant.
    const older = useAppStore.getState().domainActions.revalidateDomains()
    renderPrettyChat(`/workspaces/${WS_NEW}/chat`)

    await act(async () => {
      finishOlder([domain(WS_A, "Workspace A"), domain(WS_B, "Workspace B")])
      await older
    })

    await waitFor(() => expect(list).toHaveBeenCalledTimes(2))
    await waitFor(() => {
      expect(useAppStore.getState().activeDomainId).toBe(WS_NEW)
      expect(screen.getByTestId("path").textContent).toMatch(`/workspaces/just-added/${WS_NEW}/chat/`)
    })
  })

  it("clears the recheck timer when the page goes away mid-recheck (D3)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let release!: (domains: TenantMembership[]) => void
    try {
      vi.spyOn(workspaceApi, "list").mockReturnValue(new Promise((resolve) => { release = resolve }))
      const router = renderPrettyChat(`/workspaces/${"66666666-6666-6666-6666-666666666666"}/chat`)
      await waitFor(() => expect(workspaceApi.list).toHaveBeenCalledOnce())
      const pending = vi.getTimerCount()

      act(() => router.dispose())
      cleanup()

      expect(vi.getTimerCount()).toBeLessThan(pending)
    } finally {
      vi.useRealTimers()
      await act(async () => release(useAppStore.getState().domains))
    }
  })

  it("returns to the link when the recheck answers after the 5s fallback (D4)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let release!: (domains: TenantMembership[]) => void
    const WS_SLOW = "55555555-5555-5555-5555-555555555555"
    try {
      vi.spyOn(workspaceApi, "list").mockReturnValue(new Promise((resolve) => { release = resolve }))
      renderPrettyChat(`/workspaces/${WS_SLOW}/chat/${THREAD_STALE}`)
      await waitFor(() => expect(workspaceApi.list).toHaveBeenCalledOnce())
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000)
      })
      await waitFor(() => expect(screen.getByTestId("path").textContent).toBe(
        `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
      ))
    } finally {
      vi.useRealTimers()
    }

    await act(async () => release([
      domain(WS_SLOW, "Slow Grant"), domain(WS_A, "Workspace A"), domain(WS_B, "Workspace B"),
    ]))

    await waitFor(() => {
      expect(useAppStore.getState().activeDomainId).toBe(WS_SLOW)
      expect(screen.getByTestId("path").textContent).toBe(
        `/workspaces/slow-grant/${WS_SLOW}/chat/${THREAD_STALE}`,
      )
    })
  })

  it("stays on the fallback when the late answer comes long after it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let release!: (domains: TenantMembership[]) => void
    const WS_STALLED = "99999999-9999-9999-9999-999999999999"
    try {
      vi.spyOn(workspaceApi, "list").mockReturnValue(new Promise((resolve) => { release = resolve }))
      renderPrettyChat(`/workspaces/${WS_STALLED}/chat`)
      await waitFor(() => expect(workspaceApi.list).toHaveBeenCalledOnce())
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000)
      })
      await waitFor(() => expect(screen.getByTestId("path").textContent).toBe(
        `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
      ))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(40_000)
      })

      await act(async () => release([domain(WS_STALLED, "Stalled"), domain(WS_A, "Workspace A")]))

      expect(useAppStore.getState().activeDomainId).toBe(WS_A)
      expect(screen.getByTestId("path").textContent).toBe(
        `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it("falls back without waiting out the timeout when the recheck fails", async () => {
    vi.spyOn(workspaceApi, "list").mockRejectedValue(new Error("503"))
    const WS_BLIP = "12121212-1212-1212-1212-121212121212"
    renderPrettyChat(`/workspaces/${WS_BLIP}/chat`)

    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe(
      `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
    ), { timeout: 1_000 })
  })

  it("rechecks a link again on a later visit, A → B → A", async () => {
    const WS_NEW = "77777777-7777-7777-7777-777777777777"
    const list = vi.spyOn(workspaceApi, "list")
      .mockResolvedValueOnce([domain(WS_A, "Workspace A"), domain(WS_B, "Workspace B")])
      .mockResolvedValueOnce([
        domain(WS_NEW, "Granted Later"), domain(WS_A, "Workspace A"), domain(WS_B, "Workspace B"),
      ])
    const router = renderPrettyChat(`/workspaces/${WS_NEW}/chat`)
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe(
      `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
    ))

    await act(() => router.navigate(`/workspaces/workspace-b/${WS_B}/chat`))
    await act(() => router.navigate(`/workspaces/${WS_NEW}/chat`))

    await waitFor(() => expect(list).toHaveBeenCalledTimes(2))
    await waitFor(() => {
      expect(useAppStore.getState().activeDomainId).toBe(WS_NEW)
      expect(screen.getByTestId("path").textContent).toMatch(`/workspaces/granted-later/${WS_NEW}/chat/`)
    })
  })

  it("keeps a workspace picked mid-recheck instead of bouncing back to the link", async () => {
    const WS_NEW = "88888888-8888-8888-8888-888888888888"
    let finishRecheck!: (domains: TenantMembership[]) => void
    vi.spyOn(workspaceApi, "list").mockReturnValue(new Promise((resolve) => { finishRecheck = resolve }))
    renderPrettyChat(`/workspaces/${WS_NEW}/chat`)
    await waitFor(() => expect(workspaceApi.list).toHaveBeenCalledOnce())

    act(() => useAppStore.getState().domainActions.setActiveDomain(WS_B))
    await waitFor(() => expect(screen.getByTestId("path").textContent).toMatch(
      `/workspaces/workspace-b/${WS_B}/chat/`,
    ))

    await act(async () => {
      finishRecheck([domain(WS_NEW, "Late"), domain(WS_A, "Workspace A"), domain(WS_B, "Workspace B")])
    })

    expect(useAppStore.getState().activeDomainId).toBe(WS_B)
    expect(screen.getByTestId("path").textContent).toMatch(`/workspaces/workspace-b/${WS_B}/chat/`)
  })

  it("restores the current thread when navigation removes only the URL thread", async () => {
    const router = renderPrettyChat(`/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`)
    await act(() => router.navigate(`/workspaces/workspace-a/${WS_A}/chat`))
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe(
      `/workspaces/workspace-a/${WS_A}/chat/${THREAD_A}`,
    ))
    expect(useAppStore.getState().threadId).toBe(THREAD_A)
  })
})

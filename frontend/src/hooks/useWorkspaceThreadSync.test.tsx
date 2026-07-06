import { beforeEach, describe, expect, it } from "vitest"
import { act, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { useWorkspaceThreadSync } from "@/hooks/useWorkspaceThreadSync"
import type { TenantMembership } from "@/store/domainSlice"

// Workspace ids with EMPTY names so workspacePath yields the bare
// `/workspaces/<id>` form (no slug) and URLs are fully predictable.
const WS_A = "11111111-1111-1111-1111-111111111111"
const WS_B = "22222222-2222-2222-2222-222222222222"
const THREAD_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const THREAD_STALE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

function domain(id: string): TenantMembership {
  return {
    id,
    name: "",
    display_name: "",
    is_auto_created: false,
    role: "manage",
    tenants: [],
    member_count: 1,
    schema_status: "available",
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
  }
}

function Probe() {
  useWorkspaceThreadSync("")
  const loc = useLocation()
  return <div data-testid="path">{loc.pathname}</div>
}

describe("useWorkspaceThreadSync — no cross-workspace thread carry (00c423d)", () => {
  beforeEach(() => {
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

    // Switch workspace the same way the WorkspaceSwitcher does.
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
})

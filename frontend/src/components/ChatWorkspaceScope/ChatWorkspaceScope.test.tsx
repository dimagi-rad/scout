import { fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { beforeEach, describe, expect, it } from "vitest"

import type { WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { ChatWorkspaceScope } from "./ChatWorkspaceScope"

function workspace(tenantCount: number): WorkspaceListItem {
  return {
    id: "ws-1",
    name: "Nutrition",
    display_name: "Nutrition Program",
    is_auto_created: false,
    role: "manage",
    tenants: Array.from({ length: tenantCount }, (_, i) => ({
      id: `t${i}`,
      tenant_name: `Opp ${i}`,
      provider: "commcare_connect",
    })),
    member_count: 1,
    schema_status: "available",
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
  }
}

function renderScope(path = "/chat") {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="*" element={<ChatWorkspaceScope />} />
        <Route path="/workspaces/:slug/:id" element={<div data-testid="settings-page" />} />
        <Route path="/embed/workspaces/:slug/:id" element={<div data-testid="embed-settings-page" />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  useAppStore.setState({ domains: [workspace(2)], activeDomainId: "ws-1" })
})

describe("ChatWorkspaceScope", () => {
  it("names the active workspace and each of its sources", () => {
    renderScope()

    const scope = screen.getByTestId("chat-workspace-scope")
    expect(scope).toHaveTextContent("Nutrition Program")
    expect(screen.getByTestId("chat-workspace-scope-count")).toHaveTextContent("2 sources")
    expect(screen.getByTestId("chat-workspace-scope-source-t0")).toHaveTextContent("Opp 0")
    expect(screen.getByTestId("chat-workspace-scope-source-t1")).toHaveTextContent("Opp 1")
    expect(screen.queryByTestId("chat-workspace-scope-more")).toBeNull()
  })

  it("collapses sources past the first three into a count", () => {
    useAppStore.setState({ domains: [workspace(5)] })
    renderScope()

    expect(screen.getByTestId("chat-workspace-scope-count")).toHaveTextContent("5 sources")
    expect(screen.queryByTestId("chat-workspace-scope-source-t3")).toBeNull()
    expect(screen.getByTestId("chat-workspace-scope-more")).toHaveTextContent("+2")
    expect(screen.getByTestId("chat-workspace-scope").title).toContain("Opp 4")
  })

  it("says source in the singular for one", () => {
    useAppStore.setState({ domains: [workspace(1)] })
    renderScope()

    expect(screen.getByTestId("chat-workspace-scope-count")).toHaveTextContent("1 source")
    expect(screen.getByTestId("chat-workspace-scope-count")).not.toHaveTextContent("sources")
  })

  it("opens the workspace's settings", () => {
    renderScope()
    fireEvent.click(screen.getByTestId("chat-workspace-scope"))

    expect(screen.getByTestId("settings-page")).toBeInTheDocument()
  })

  it("keeps the embed prefix", () => {
    renderScope("/embed/chat")
    fireEvent.click(screen.getByTestId("chat-workspace-scope"))

    expect(screen.getByTestId("embed-settings-page")).toBeInTheDocument()
  })

  it("renders nothing before the workspace list has it", () => {
    useAppStore.setState({ domains: [] })
    renderScope()

    expect(screen.queryByTestId("chat-workspace-scope")).toBeNull()
  })
})

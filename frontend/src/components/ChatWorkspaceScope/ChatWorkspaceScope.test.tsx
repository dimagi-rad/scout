import { fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { beforeEach, describe, expect, it } from "vitest"

import type { WorkspaceListItem } from "@/api/workspaces"
import { EmbedSettingsProvider } from "@/contexts/EmbedSettingsContext"
import type { EmbedMode } from "@/hooks/useEmbedParams"
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

function renderScope(path = "/chat", embedMode: EmbedMode = "full") {
  const routes = (
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="*" element={<ChatWorkspaceScope />} />
        <Route path="/workspaces/:slug/:id" element={<div data-testid="settings-page" />} />
        <Route path="/embed/workspaces/:slug/:id" element={<div data-testid="embed-settings-page" />} />
      </Routes>
    </MemoryRouter>
  )
  render(
    path.startsWith("/embed") ? (
      <EmbedSettingsProvider initialMode={embedMode} initialTheme="light">
        {routes}
      </EmbedSettingsProvider>
    ) : (
      routes
    ),
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
    expect(screen.getByTestId("chat-workspace-scope")).toHaveAccessibleName(
      "Workspace Nutrition Program, 5 sources: Opp 0, Opp 1, Opp 2, Opp 3, Opp 4. Open workspace settings.",
    )
  })

  it("says when the workspace has no sources", () => {
    useAppStore.setState({ domains: [workspace(0)] })
    renderScope()

    expect(screen.getByTestId("chat-workspace-scope-count")).toHaveTextContent("0 sources")
    expect(screen.getByTestId("chat-workspace-scope")).toHaveAccessibleName(
      "Workspace Nutrition Program, 0 sources: none. Open workspace settings.",
    )
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

  it("is not a link in an embed without the sidebar, which has no way back", () => {
    renderScope("/embed/chat", "chat")

    const scope = screen.getByTestId("chat-workspace-scope")
    expect(scope.tagName).toBe("DIV")
    expect(scope).toHaveTextContent("Nutrition Program")
  })

  it("uses the neutral icon when sources span providers", () => {
    const mixed = workspace(2)
    mixed.tenants[1] = { ...mixed.tenants[1], provider: "ocs" }
    useAppStore.setState({ domains: [mixed] })
    renderScope()

    expect(screen.getByTestId("chat-workspace-scope-source-t1").className).toContain("purple")
    expect(screen.getByTestId("chat-workspace-scope").querySelector("svg.lucide-database")).not.toBeNull()
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

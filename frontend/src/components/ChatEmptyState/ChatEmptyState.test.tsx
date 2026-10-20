import { fireEvent, render, screen } from "@testing-library/react"
import { beforeEach, expect, it, vi } from "vitest"
import type { WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { ChatEmptyState } from "./ChatEmptyState"

function workspace(tenantNames: string[]): WorkspaceListItem {
  return {
    id: "ws",
    name: "ws",
    display_name: "CHC program",
    is_auto_created: false,
    role: "manage",
    tenants: tenantNames.map((name, i) => ({
      id: `t-${i}`,
      tenant_name: name,
      provider: "commcare_connect",
    })),
    member_count: 1,
    schema_status: "available",
    last_synced_at: null,
    created_at: "2026-01-01T12:00:00Z",
  }
}

function renderEmptyState(ws: WorkspaceListItem) {
  useAppStore.setState({ domains: [ws], activeDomainId: ws.id })
  render(<ChatEmptyState input="" setInput={vi.fn()} onSend={vi.fn()} />)
}

beforeEach(() => {
  useAppStore.setState(useAppStore.getInitialState(), true)
})

it("says which workspace and sources a question will go to", () => {
  renderEmptyState(workspace(["Kenya", "Uganda", "Malawi", "Nigeria"]))

  expect(screen.getByTestId("chat-workspace-context")).toHaveTextContent(
    "Asking about CHC program · Sources: Kenya, Uganda, Malawi +1 more Change",
  )
  expect(screen.getByTestId("chat-workspace-sources")).toHaveAttribute(
    "title",
    "4 data sources: Kenya, Uganda, Malawi, Nigeria",
  )
})

it("opens the workspace switcher from Change", () => {
  renderEmptyState(workspace(["Kenya"]))
  const before = useAppStore.getState().workspaceSwitcherRequest

  fireEvent.click(screen.getByTestId("chat-workspace-change"))

  expect(useAppStore.getState().workspaceSwitcherRequest).toBe(before + 1)
})

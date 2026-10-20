import { render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import { workspaceApi, type WorkspaceDetail, type WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { WorkspaceDetailPage } from "./WorkspaceDetailPage"

const listed: WorkspaceListItem = {
  id: "ws-1", name: "Nutrition", display_name: "Nutrition", has_access: true,
  is_auto_created: false, role: "read", tenants: [], member_count: 1,
  schema_status: "available", last_synced_at: null, created_at: "2026-01-01T00:00:00Z",
}
const detail: WorkspaceDetail = {
  id: "ws-1", name: "Nutrition", display_name: "Nutrition", is_auto_created: false,
  role: "read", member_count: 1, schema_status: "available", last_synced_at: null,
  created_at: "2026-01-01T00:00:00Z", system_prompt: "", tenant_count: 0, updated_at: "",
}

beforeEach(() => {
  vi.spyOn(workspaceApi, "getDetail").mockResolvedValue(detail)
  vi.spyOn(workspaceApi, "getTenants").mockResolvedValue([])
  vi.spyOn(workspaceApi, "getMembers").mockResolvedValue({ members: [], invites: [] })
  vi.spyOn(workspaceApi, "list").mockResolvedValue([listed])
  useAppStore.setState({ domains: [listed], domainsStatus: "loaded", activeDomainId: "ws-1" })
})

afterEach(() => vi.restoreAllMocks())

it.each([
  ["/workspaces/nutrition/ws-1", "/workspaces"],
  ["/embed/workspaces/nutrition/ws-1", "/embed/workspaces"],
])("links back to the workspace list under the same prefix (%s)", async (path, back) => {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/workspaces/:slug/:workspaceId" element={<WorkspaceDetailPage />} />
        <Route path="/embed/workspaces/:slug/:workspaceId" element={<WorkspaceDetailPage />} />
      </Routes>
    </MemoryRouter>,
  )

  expect(await screen.findByTestId("back-to-workspaces")).toHaveAttribute("href", back)
})

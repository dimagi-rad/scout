import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { expect, it, vi } from "vitest"

import { workspaceApi } from "@/api/workspaces"
import { getUserTenantsCached } from "@/api/userTenantsCache"
import { TenantsTab } from "./WorkspaceDetailPage"

vi.mock("@/api/workspaces", () => ({ workspaceApi: { getTenants: vi.fn() } }))
vi.mock("@/api/userTenantsCache", () => ({
  getUserTenantsCached: vi.fn(),
  refreshUserTenants: vi.fn(),
}))
vi.mock("@/store/store", () => {
  const state = { user: { id: "user" } }
  return { useAppStore: (selector: (value: typeof state) => unknown) => selector(state) }
})

it("lists available sources alphabetically, whatever order the server sends", async () => {
  vi.mocked(workspaceApi.getTenants).mockResolvedValue([
    { id: "wt-c", tenant_id: "tenant-c", tenant_name: "Charlie", provider: "commcare" },
  ])
  // The server orders by -last_selected_at, which puts never-selected (NULL) rows first (#357).
  vi.mocked(getUserTenantsCached).mockResolvedValue([
    { id: "m-z", tenant_uuid: "tenant-z", provider: "commcare", tenant_id: "z", tenant_name: "Zulu", last_selected_at: "2026-09-01T00:00:00Z" },
    { id: "m-b", tenant_uuid: "tenant-b", provider: "ocs", tenant_id: "b", tenant_name: "bravo", last_selected_at: null },
    { id: "m-c", tenant_uuid: "tenant-c", provider: "commcare", tenant_id: "c", tenant_name: "Charlie", last_selected_at: null },
    { id: "m-a", tenant_uuid: "tenant-a", provider: "commcare", tenant_id: "a", tenant_name: "Alpha", last_selected_at: null },
  ])
  render(<TenantsTab workspaceId="ws-1" isManager />)

  await userEvent.click(await screen.findByTestId("add-tenant-btn"))
  await screen.findByTestId("available-sources-list")

  const order = [
    ...screen.getByTestId("available-sources-list").querySelectorAll("[data-testid^='add-tenant-']"),
  ].map((el) => el.getAttribute("data-testid"))
  expect(order).toEqual(["add-tenant-tenant-a", "add-tenant-tenant-b", "add-tenant-tenant-z"])
})

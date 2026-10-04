import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, expect, it, vi } from "vitest"

import { ApiError } from "@/api/client"
import { workspaceApi } from "@/api/workspaces"
import { getUserTenantsCached } from "@/api/userTenantsCache"
import { TenantsTab } from "./TenantsTab"

vi.mock("@/api/workspaces", () => ({
  workspaceApi: { getTenants: vi.fn(), removeTenant: vi.fn() },
}))
vi.mock("@/api/userTenantsCache", () => ({
  getUserTenantsCached: vi.fn(),
  refreshUserTenants: vi.fn(),
}))
vi.mock("@/store/store", () => {
  const state = { user: { id: "user" }, accountSession: { isCurrent: () => true } }
  return { useAppStore: (selector: (value: typeof state) => unknown) => selector(state) }
})

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getUserTenantsCached).mockResolvedValue([])
})

it("lists available sources alphabetically, whatever order the server sends", async () => {
  vi.mocked(workspaceApi.getTenants).mockResolvedValue([
    { id: "wt-c", tenant_id: "tenant-c", tenant_name: "Charlie", provider: "commcare" },
  ])
  vi.mocked(getUserTenantsCached).mockResolvedValue([
    { id: "m-z", tenant_uuid: "tenant-z", provider: "commcare", tenant_id: "z", tenant_name: "Zulu", last_selected_at: "2026-09-01T00:00:00Z" },
    { id: "m-b", tenant_uuid: "tenant-b", provider: "ocs", tenant_id: "b", tenant_name: "bravo", last_selected_at: null },
    { id: "m-c", tenant_uuid: "tenant-c", provider: "commcare", tenant_id: "c", tenant_name: "Charlie", last_selected_at: null },
    { id: "m-a", tenant_uuid: "tenant-a", provider: "commcare", tenant_id: "a", tenant_name: "Alpha", last_selected_at: null },
  ])
  render(<TenantsTab workspaceId="ws-1" isManager onWorkspaceDeleted={vi.fn()} />)

  await userEvent.click(await screen.findByTestId("add-tenant-btn"))
  await screen.findByTestId("available-sources-list")

  const order = [
    ...screen.getByTestId("available-sources-list").querySelectorAll("[data-testid^='add-tenant-']"),
  ].map((el) => el.getAttribute("data-testid"))
  expect(order).toEqual(["add-tenant-tenant-a", "add-tenant-tenant-b", "add-tenant-tenant-z"])
})

const alpha = { id: "wt-a", tenant_id: "tenant-a", tenant_name: "Alpha", provider: "commcare" }
const bravo = { id: "wt-b", tenant_id: "tenant-b", tenant_name: "Bravo", provider: "commcare" }

it("confirms deleting the workspace before removing its last source (#381)", async () => {
  vi.mocked(workspaceApi.getTenants).mockResolvedValue([alpha])
  vi.mocked(workspaceApi.removeTenant).mockResolvedValue({ workspace_deleted: true })
  const onWorkspaceDeleted = vi.fn()
  render(<TenantsTab workspaceId="ws-1" isManager onWorkspaceDeleted={onWorkspaceDeleted} />)

  await userEvent.click(await screen.findByTestId("remove-tenant-wt-a"))

  const dialog = await screen.findByTestId("last-source-dialog")
  expect(dialog).toHaveTextContent("deletes the workspace, its conversations and its data")
  expect(workspaceApi.removeTenant).not.toHaveBeenCalled()

  await userEvent.click(screen.getByTestId("last-source-confirm"))

  await waitFor(() => expect(onWorkspaceDeleted).toHaveBeenCalledOnce())
  expect(workspaceApi.removeTenant).toHaveBeenCalledExactlyOnceWith("ws-1", "wt-a", {
    confirmDeleteWorkspace: true,
  })
})

it("cancelling the last-source dialog removes nothing", async () => {
  vi.mocked(workspaceApi.getTenants).mockResolvedValue([alpha])
  const onWorkspaceDeleted = vi.fn()
  render(<TenantsTab workspaceId="ws-1" isManager onWorkspaceDeleted={onWorkspaceDeleted} />)

  await userEvent.click(await screen.findByTestId("remove-tenant-wt-a"))
  await userEvent.click(await screen.findByTestId("last-source-cancel"))

  await waitFor(() => expect(screen.queryByTestId("last-source-dialog")).not.toBeInTheDocument())
  expect(workspaceApi.removeTenant).not.toHaveBeenCalled()
  expect(onWorkspaceDeleted).not.toHaveBeenCalled()
})

it("asks to delete the workspace when the server says the source became the last one", async () => {
  vi.mocked(workspaceApi.getTenants).mockResolvedValue([alpha, bravo])
  vi.mocked(workspaceApi.removeTenant).mockRejectedValueOnce(
    new ApiError(409, "Removing the last data source deletes the workspace.", {
      requires_confirmation: "delete_workspace",
    }),
  )
  render(<TenantsTab workspaceId="ws-1" isManager onWorkspaceDeleted={vi.fn()} />)

  await userEvent.click(await screen.findByTestId("remove-tenant-wt-a"))
  await userEvent.click(screen.getByTestId("confirm-remove-tenant-wt-a"))

  expect(await screen.findByTestId("last-source-dialog")).toBeInTheDocument()
  expect(workspaceApi.removeTenant).toHaveBeenCalledExactlyOnceWith("ws-1", "wt-a")
})

it("keeps the workspace and reloads its sources when one was added before confirming", async () => {
  vi.mocked(workspaceApi.getTenants)
    .mockResolvedValueOnce([alpha])
    .mockResolvedValueOnce([bravo])
  vi.mocked(workspaceApi.removeTenant).mockResolvedValue(undefined)
  const onWorkspaceDeleted = vi.fn()
  render(<TenantsTab workspaceId="ws-1" isManager onWorkspaceDeleted={onWorkspaceDeleted} />)

  await userEvent.click(await screen.findByTestId("remove-tenant-wt-a"))
  await userEvent.click(await screen.findByTestId("last-source-confirm"))

  expect(await screen.findByTestId("tenant-row-wt-b")).toBeInTheDocument()
  expect(screen.queryByTestId("tenant-row-wt-a")).not.toBeInTheDocument()
  expect(onWorkspaceDeleted).not.toHaveBeenCalled()
})

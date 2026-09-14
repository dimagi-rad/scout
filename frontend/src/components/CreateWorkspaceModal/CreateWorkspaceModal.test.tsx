import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { beforeEach, expect, it, vi } from "vitest"

import { workspaceApi } from "@/api/workspaces"
import { getUserTenantsCached } from "@/api/userTenantsCache"
import { CreateWorkspaceModal } from "./CreateWorkspaceModal"

vi.mock("@/api/workspaces", () => ({ workspaceApi: { create: vi.fn() } }))
vi.mock("@/api/userTenantsCache", () => ({ getUserTenantsCached: vi.fn() }))
vi.mock("@/store/store", () => {
  const state = {
    user: { id: "user" }, domains: [], domainsStatus: "loaded",
    domainActions: { fetchDomains: vi.fn(), setActiveDomain: vi.fn() },
  }
  return { useAppStore: (selector: (value: typeof state) => unknown) => selector(state) }
})

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getUserTenantsCached).mockResolvedValue([
    { id: "membership-a", tenant_uuid: "tenant-a", provider: "commcare", tenant_id: "a", tenant_name: "Alpha", last_selected_at: null },
    { id: "membership-b", tenant_uuid: "tenant-b", provider: "ocs", tenant_id: "b", tenant_name: "Beta", last_selected_at: null },
  ])
  vi.mocked(workspaceApi.create).mockResolvedValue({
    id: "workspace", name: "Analysis",
  })
})

async function openModal() {
  const user = userEvent.setup()
  const onClose = vi.fn()
  render(<MemoryRouter><CreateWorkspaceModal onClose={onClose} /></MemoryRouter>)
  await screen.findByTestId("create-source-tenant-a")
  await user.type(screen.getByLabelText("Name"), "Analysis")
  return { user, onClose }
}

it("filters across providers and All without creating a workspace", async () => {
  const { user } = await openModal()
  await user.click(screen.getByTestId("filter-provider-commcare"))
  expect(screen.queryByTestId("create-source-tenant-b")).not.toBeInTheDocument()
  await user.click(screen.getByTestId("filter-provider-ocs"))
  expect(screen.queryByTestId("create-source-tenant-a")).not.toBeInTheDocument()
  await user.click(screen.getByTestId("filter-provider-all"))
  expect(screen.getByTestId("create-source-tenant-a")).toBeInTheDocument()
  expect(screen.getByTestId("create-source-tenant-b")).toBeInTheDocument()
  expect(workspaceApi.create).not.toHaveBeenCalled()
})

it("searches on Enter without creating a workspace", async () => {
  const { user } = await openModal()
  await user.type(screen.getByTestId("search-filter-input"), "Alpha{Enter}")
  expect(screen.getByTestId("create-source-tenant-a")).toBeInTheDocument()
  expect(screen.queryByTestId("create-source-tenant-b")).not.toBeInTheDocument()
  expect(workspaceApi.create).not.toHaveBeenCalled()
})

it.each([false, true])("deliberate Create works with selected source: %s", async (selectSource) => {
  const { user, onClose } = await openModal()
  if (selectSource) await user.click(screen.getByTestId("create-source-tenant-a"))
  await user.click(screen.getByTestId("create-workspace-submit"))
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  expect(workspaceApi.create).toHaveBeenCalledExactlyOnceWith("Analysis", selectSource ? ["tenant-a"] : [])
})

it("preserves keyboard submission from the workspace name", async () => {
  const { user, onClose } = await openModal()
  await user.click(screen.getByLabelText("Name"))
  await user.keyboard("{Enter}")
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  expect(workspaceApi.create).toHaveBeenCalledExactlyOnceWith("Analysis", [])
})

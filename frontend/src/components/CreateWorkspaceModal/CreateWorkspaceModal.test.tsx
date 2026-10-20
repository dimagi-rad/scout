import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"
import { beforeEach, expect, it, vi } from "vitest"

import { workspaceApi } from "@/api/workspaces"
import { getUserTenantsCached, refreshUserTenants } from "@/api/userTenantsCache"
import { CreateWorkspaceModal } from "./CreateWorkspaceModal"

vi.mock("@/api/workspaces", () => ({ workspaceApi: { create: vi.fn() } }))
vi.mock("@/api/userTenantsCache", () => ({
  getUserTenantsCached: vi.fn(),
  refreshUserTenants: vi.fn(),
}))
const storeActions = vi.hoisted(() => ({
  fetchDomains: vi.fn(), setActiveDomain: vi.fn(), newThread: vi.fn(),
}))
vi.mock("@/store/store", () => {
  const state = {
    user: { id: "user" }, domains: [], domainsStatus: "loaded",
    accountSession: { isCurrent: () => true },
    domainActions: {
      fetchDomains: storeActions.fetchDomains, setActiveDomain: storeActions.setActiveDomain,
    },
    uiActions: { newThread: storeActions.newThread },
  }
  return { useAppStore: (selector: (value: typeof state) => unknown) => selector(state) }
})

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.mocked(getUserTenantsCached).mockResolvedValue([
    { id: "membership-a", tenant_uuid: "tenant-a", provider: "commcare", tenant_id: "a", tenant_name: "Alpha", last_selected_at: null },
    { id: "membership-b", tenant_uuid: "tenant-b", provider: "ocs", tenant_id: "b", tenant_name: "Beta", last_selected_at: null },
  ])
  vi.mocked(workspaceApi.create).mockResolvedValue({
    id: "workspace", name: "Analysis",
  })
})

function CurrentPath() {
  return <div data-testid="current-path">{useLocation().pathname}</div>
}

async function openModal(path = "/workspaces") {
  const user = userEvent.setup()
  const onClose = vi.fn()
  render(
    <MemoryRouter initialEntries={[path]}>
      <CreateWorkspaceModal onClose={onClose} />
      <Routes><Route path="*" element={<CurrentPath />} /></Routes>
    </MemoryRouter>,
  )
  await screen.findByTestId("create-sources-list")
  await user.type(screen.getByLabelText("Name"), "Analysis")
  return { user, onClose }
}

const F = "create-sources-filter"
const STORAGE_KEY = "scout:source-filters:v1:user"

function connect(id: string, name: string, attributes: Record<string, unknown>) {
  return {
    id: `membership-${id}`, tenant_uuid: `tenant-${id}`, provider: "commcare_connect",
    tenant_id: id, tenant_name: name, last_selected_at: null, attributes,
  }
}

const mixedSources = [
  { id: "membership-a", tenant_uuid: "tenant-a", provider: "commcare", tenant_id: "a", tenant_name: "Alpha", last_selected_at: null, attributes: {} },
  connect("c1", "Kenya Live", { is_active: true, is_test: false, organization: "dimagi", organization_name: "Dimagi" }),
  connect("c2", "Kenya Old", { is_active: false, is_test: false, organization: "dimagi", organization_name: "Dimagi" }),
  connect("c3", "Ghana Test", { is_active: true, is_test: true, organization: "acme", organization_name: "Acme" }),
]

function shown() {
  return [...screen.getByTestId("create-sources-list").querySelectorAll("[data-testid^='create-source-']")]
    .map((el) => el.getAttribute("data-testid")!.replace("create-source-tenant-", ""))
}

it("filters by provider through the facet popover without creating a workspace", async () => {
  const { user } = await openModal()
  await user.click(screen.getByTestId(`${F}-facet-provider`))
  await user.click(await screen.findByTestId(`${F}-facet-provider-option-commcare`))
  expect(shown()).toEqual(["a"])
  expect(screen.getByTestId(`${F}-facet-provider`)).toHaveTextContent("Provider: CommCare")
  await user.click(screen.getByTestId(`${F}-facet-provider-option-ocs`))
  expect(shown()).toEqual(["a", "b"])
  expect(screen.getByTestId(`${F}-facet-provider`)).toHaveTextContent("Provider: 2")
  await user.click(screen.getByTestId(`${F}-facet-provider-only-ocs`))
  expect(shown()).toEqual(["b"])
  expect(screen.getByTestId(`${F}-count`)).toHaveTextContent("Showing 1 of 2")
  await user.click(screen.getByTestId(`${F}-clear`))
  expect(shown()).toEqual(["a", "b"])
  expect(workspaceApi.create).not.toHaveBeenCalled()
})

it("does not offer Connect facets when no Connect source is present", async () => {
  await openModal()
  expect(screen.getByTestId(`${F}-facet-provider`)).toBeInTheDocument()
  expect(screen.queryByTestId(`${F}-facet-status`)).toBeNull()
})

it("combines Connect facets with the search, never hiding CommCare rows", async () => {
  vi.mocked(getUserTenantsCached).mockResolvedValue(mixedSources)
  const { user } = await openModal()

  await user.click(screen.getByTestId(`${F}-facet-status`))
  await user.click(await screen.findByTestId(`${F}-facet-status-option-active`))
  expect(shown()).toEqual(["a", "c3", "c1"])
  // Counts reflect the other filters: with Status=Active, Dimagi has one row left.
  await user.click(screen.getByTestId(`${F}-facet-organization`))
  expect(await screen.findByTestId(`${F}-facet-organization-popover`)).toHaveTextContent(/Dimagi.*1/)

  await user.type(screen.getByTestId("search-filter-input"), "kenya")
  expect(shown()).toEqual(["c1"])
  expect(screen.getByTestId(`${F}-count`)).toHaveTextContent("Showing 1 of 4")
})

it("keeps a selection that a filter hides, and says so", async () => {
  vi.mocked(getUserTenantsCached).mockResolvedValue(mixedSources)
  const { user, onClose } = await openModal()
  await user.click(screen.getByTestId("create-source-tenant-c2"))
  await user.click(screen.getByTestId("create-source-tenant-c1"))

  await user.click(screen.getByTestId(`${F}-facet-status`))
  await user.click(await screen.findByTestId(`${F}-facet-status-only-active`))

  expect(shown()).not.toContain("c2")
  expect(screen.getByTestId("create-sources-selected")).toHaveTextContent(
    "2 selected · 1 hidden by filters",
  )
  await user.keyboard("{Escape}")
  await user.click(screen.getByTestId("create-workspace-submit"))
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  expect(workspaceApi.create).toHaveBeenCalledExactlyOnceWith("Analysis", ["tenant-c2", "tenant-c1"])
})

it("restores persisted facets and offers Clear filters when nothing matches", async () => {
  vi.mocked(getUserTenantsCached).mockResolvedValue(mixedSources)
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ type: ["test"], provider: ["commcare_connect"] }))
  const { user } = await openModal()

  expect(shown()).toEqual(["c3"])
  expect(screen.getByTestId(`${F}-facet-type`)).toHaveTextContent("Type: Test")

  await user.type(screen.getByTestId("search-filter-input"), "kenya")
  expect(screen.getByTestId("create-sources-list")).toHaveTextContent("No data sources match")
  await user.click(screen.getByTestId(`${F}-empty-clear`))

  expect(shown()).toEqual(["a", "c3", "c1", "c2"])
  expect(screen.getByTestId("search-filter-input")).toHaveValue("")
  expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
})

it("drops a persisted facet value the refreshed list no longer has", async () => {
  vi.mocked(getUserTenantsCached).mockResolvedValue(mixedSources)
  vi.mocked(refreshUserTenants).mockResolvedValue(mixedSources.filter((t) => t.tenant_id !== "c3"))
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ organization: ["acme"] }))
  const { user } = await openModal()
  expect(shown()).toEqual(["a", "c3"])

  await user.click(screen.getByTestId("create-sources-refresh"))

  await waitFor(() => expect(shown()).toEqual(["a", "c1", "c2"]))
  expect(screen.getByTestId(`${F}-facet-organization`)).toHaveTextContent(/^Org$/)
})

it("searches on Enter without creating a workspace", async () => {
  const { user } = await openModal()
  await user.type(screen.getByTestId("search-filter-input"), "Alpha{Enter}")
  expect(screen.getByTestId("create-source-tenant-a")).toBeInTheDocument()
  expect(screen.queryByTestId("create-source-tenant-b")).not.toBeInTheDocument()
  expect(workspaceApi.create).not.toHaveBeenCalled()
})

it("creates with the selected source", async () => {
  const { user, onClose } = await openModal()
  await user.click(screen.getByTestId("create-source-tenant-a"))
  await user.click(screen.getByTestId("create-workspace-submit"))
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  expect(workspaceApi.create).toHaveBeenCalledExactlyOnceWith("Analysis", ["tenant-a"])
})

it("opens a fresh chat in the new workspace", async () => {
  const { user } = await openModal()
  await user.click(screen.getByTestId("create-source-tenant-a"))
  await user.click(screen.getByTestId("create-workspace-submit"))

  await waitFor(() =>
    expect(screen.getByTestId("current-path")).toHaveTextContent("/workspaces/analysis/workspace/chat"),
  )
  expect(storeActions.setActiveDomain).toHaveBeenCalledExactlyOnceWith("workspace")
  expect(storeActions.newThread).toHaveBeenCalledOnce()
})

it("keeps the embed prefix when opening the new workspace's chat", async () => {
  const { user } = await openModal("/embed/workspaces")
  await user.click(screen.getByTestId("create-source-tenant-a"))
  await user.click(screen.getByTestId("create-workspace-submit"))

  await waitFor(() =>
    expect(screen.getByTestId("current-path")).toHaveTextContent(
      "/embed/workspaces/analysis/workspace/chat",
    ),
  )
})

it("refuses to create a workspace with no source (#381)", async () => {
  const { user } = await openModal()
  expect(screen.getByTestId("create-workspace-submit")).toBeDisabled()
  await user.click(screen.getByLabelText("Name"))
  await user.keyboard("{Enter}")
  expect(workspaceApi.create).not.toHaveBeenCalled()
})

it("preserves keyboard submission from the workspace name", async () => {
  const { user, onClose } = await openModal()
  await user.click(screen.getByTestId("create-source-tenant-a"))
  await user.click(screen.getByLabelText("Name"))
  await user.keyboard("{Enter}")
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  expect(workspaceApi.create).toHaveBeenCalledExactlyOnceWith("Analysis", ["tenant-a"])
})

it("lists data sources alphabetically, whatever order the server sends", async () => {
  vi.mocked(getUserTenantsCached).mockResolvedValue([
    { id: "m-z", tenant_uuid: "tenant-z", provider: "commcare", tenant_id: "z", tenant_name: "Zulu", last_selected_at: "2026-09-01T00:00:00Z" },
    { id: "m-b", tenant_uuid: "tenant-b", provider: "ocs", tenant_id: "b", tenant_name: "bravo", last_selected_at: null },
    { id: "m-a", tenant_uuid: "tenant-a", provider: "commcare", tenant_id: "a", tenant_name: "Alpha", last_selected_at: null },
  ])
  render(<MemoryRouter><CreateWorkspaceModal onClose={vi.fn()} /></MemoryRouter>)
  await screen.findByTestId("create-source-tenant-a")
  const order = [...screen.getByTestId("create-sources-list").querySelectorAll("[data-testid^='create-source-']")]
    .map((el) => el.getAttribute("data-testid"))
  expect(order).toEqual(["create-source-tenant-a", "create-source-tenant-b", "create-source-tenant-z"])
})

it("offers a retry when the data sources fail to load, since one is required", async () => {
  vi.mocked(getUserTenantsCached)
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValueOnce([
      { id: "membership-a", tenant_uuid: "tenant-a", provider: "commcare", tenant_id: "a", tenant_name: "Alpha", last_selected_at: null },
    ])
  const user = userEvent.setup()
  render(<MemoryRouter><CreateWorkspaceModal onClose={vi.fn()} /></MemoryRouter>)

  expect(await screen.findByTestId("create-sources-error")).toHaveTextContent("Failed to load data sources")
  await user.click(screen.getByTestId("create-sources-retry"))

  expect(await screen.findByTestId("create-source-tenant-a")).toBeInTheDocument()
})

it("refreshes upstream sources on request so a newly granted one appears", async () => {
  vi.mocked(refreshUserTenants).mockResolvedValue([
    { id: "membership-a", tenant_uuid: "tenant-a", provider: "commcare", tenant_id: "a", tenant_name: "Alpha", last_selected_at: null },
    { id: "membership-c", tenant_uuid: "tenant-c", provider: "commcare_connect", tenant_id: "c", tenant_name: "Gamma", last_selected_at: null },
  ])
  const { user } = await openModal()

  await user.click(screen.getByTestId("create-sources-refresh"))

  expect(await screen.findByTestId("create-source-tenant-c")).toBeInTheDocument()
  expect(refreshUserTenants).toHaveBeenCalledExactlyOnceWith("user")
})

it("keeps the loaded sources usable when a refresh fails", async () => {
  vi.mocked(refreshUserTenants).mockRejectedValue(new Error("timeout"))
  const { user } = await openModal()

  await user.click(screen.getByTestId("create-sources-refresh"))

  expect(await screen.findByTestId("create-sources-refresh-error")).toBeInTheDocument()
  expect(screen.getByTestId("create-source-tenant-a")).toBeInTheDocument()
})

it("drops a selection the refreshed list no longer offers", async () => {
  vi.mocked(refreshUserTenants).mockResolvedValue([
    { id: "membership-b", tenant_uuid: "tenant-b", provider: "ocs", tenant_id: "b", tenant_name: "Beta", last_selected_at: null },
  ])
  const { user } = await openModal()
  await user.click(screen.getByTestId("create-source-tenant-a"))

  await user.click(screen.getByTestId("create-sources-refresh"))

  await waitFor(() => expect(screen.queryByTestId("create-source-tenant-a")).toBeNull())
  expect(screen.getByTestId("create-workspace-submit")).toBeDisabled()
})

it("shows the refreshed sources after the initial load failed", async () => {
  vi.mocked(getUserTenantsCached).mockRejectedValue(new Error("offline"))
  vi.mocked(refreshUserTenants).mockResolvedValue([
    { id: "membership-a", tenant_uuid: "tenant-a", provider: "commcare", tenant_id: "a", tenant_name: "Alpha", last_selected_at: null },
  ])
  const user = userEvent.setup()
  render(<MemoryRouter><CreateWorkspaceModal onClose={vi.fn()} /></MemoryRouter>)
  await screen.findByTestId("create-sources-error")

  await user.click(screen.getByTestId("create-sources-refresh"))

  expect(await screen.findByTestId("create-source-tenant-a")).toBeInTheDocument()
  expect(screen.queryByTestId("create-sources-error")).toBeNull()
})

it("narrows to a pasted id list, names the ids it cannot find, and selects all shown", async () => {
  vi.mocked(getUserTenantsCached).mockResolvedValue([
    connect("523", "Nama Wellness", { is_active: false }),
    connect("1234", "KMC GHI", { is_active: true }),
    connect("1236", "KMC EHA", { is_active: true }),
  ])
  const { user, onClose } = await openModal()

  await user.click(screen.getByTestId("search-filter-input"))
  await user.paste("523, 1236, 759")
  expect(shown()).toEqual(["1236", "523"])
  expect(screen.getByTestId("create-sources-id-list-shown")).toHaveTextContent("2 of 3 IDs shown")
  expect(screen.getByTestId("create-sources-id-list-not-found")).toHaveTextContent("1 not found: 759")

  await user.click(screen.getByTestId("create-sources-select-shown"))
  expect(screen.getByTestId("create-sources-selected")).toHaveTextContent("2 selected")
  expect(screen.queryByTestId("create-sources-select-shown")).toBeNull()
  await user.click(screen.getByTestId("create-workspace-submit"))
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  expect(workspaceApi.create).toHaveBeenCalledExactlyOnceWith("Analysis", ["tenant-1236", "tenant-523"])
})

it("reads a pasted column of ids, one per line, as a list and names a typo in it", async () => {
  vi.mocked(getUserTenantsCached).mockResolvedValue([
    connect("523", "Nama Wellness", { is_active: true }),
    connect("1236", "KMC EHA", { is_active: true }),
  ])
  const { user } = await openModal()

  await user.click(screen.getByTestId("search-filter-input"))
  await user.paste("523\r\n1236\r\n67S\r\n")
  expect(screen.getByTestId("search-filter-input")).toHaveValue("523, 1236, 67S")
  expect(shown()).toEqual(["1236", "523"])
  expect(screen.getByTestId("create-sources-id-list-shown")).toHaveTextContent("2 IDs shown")
  expect(screen.getByTestId("create-sources-id-list-unrecognised")).toHaveTextContent("1 not an ID: 67S")

  await user.paste("700\n701")
  expect(screen.getByTestId("search-filter-input")).toHaveValue("523, 1236, 67S, 700, 701")
})

it("names pasted ids a remembered facet hides, and clears only the facets", async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ status: ["active"] }))
  vi.mocked(getUserTenantsCached).mockResolvedValue([
    connect("523", "Nama Wellness", { is_active: false }),
    connect("1236", "KMC EHA", { is_active: true }),
  ])
  const { user } = await openModal()

  await user.click(screen.getByTestId("search-filter-input"))
  await user.paste("523, 1236")
  expect(shown()).toEqual(["1236"])
  expect(screen.getByTestId("create-sources-id-list-shown")).toHaveTextContent("1 of 2 IDs shown")
  expect(screen.getByTestId("create-sources-id-list-hidden")).toHaveTextContent("1 hidden by filters: 523")
  expect(screen.getByTestId("create-sources-select-shown")).toHaveTextContent("Select 1 shown")

  // Screen readers hear every reason line, but not the buttons beside them.
  const status = within(screen.getByTestId("create-sources-id-list")).getByRole("status")
  expect(within(status).getByTestId("create-sources-id-list-hidden")).toBeInTheDocument()
  expect(within(status).queryByTestId("create-sources-id-list-clear-facets")).toBeNull()
  expect(within(status).queryByTestId("create-sources-select-shown")).toBeNull()

  await user.click(screen.getByTestId("create-sources-id-list-clear-facets"))
  expect(shown()).toEqual(["1236", "523"])
  expect(screen.getByTestId("search-filter-input")).toHaveValue("523, 1236")
  expect(screen.getByTestId("create-sources-id-list-shown")).toHaveTextContent("All 2 IDs shown")
})

it("selects every source a program filter shows, keeping earlier picks", async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ program: ["7"] }))
  vi.mocked(getUserTenantsCached).mockResolvedValue([
    connect("p1", "Malaria North", { program: "7", program_name: "Malaria" }),
    connect("p2", "Malaria South", { program: "7", program_name: "Malaria" }),
    connect("q1", "Nutrition East", { program: "8", program_name: "Nutrition" }),
  ])
  const { user, onClose } = await openModal()
  expect(shown()).toEqual(["p1", "p2"])

  await user.click(screen.getByTestId("create-source-tenant-p1"))
  expect(screen.getByTestId("create-sources-select-shown")).toHaveTextContent("Select 1 shown")
  await user.click(screen.getByTestId("create-sources-select-shown"))

  expect(screen.getByTestId("create-sources-selected")).toHaveTextContent("2 selected")
  expect(screen.queryByTestId("create-sources-select-shown")).toBeNull()
  await user.click(screen.getByTestId("create-workspace-submit"))
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  expect(workspaceApi.create).toHaveBeenCalledExactlyOnceWith("Analysis", ["tenant-p1", "tenant-p2"])
})

it("does not offer to select the whole unfiltered list", async () => {
  await openModal()
  expect(screen.queryByTestId("create-sources-select-shown")).toBeNull()
})

it("warns, without blocking, when the pick has more Connect opportunities than the quick check covers", async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ program: ["7"] }))
  vi.mocked(getUserTenantsCached).mockResolvedValue([
    ...Array.from({ length: 26 }, (_, i) =>
      connect(`${100 + i}`, `Opp ${100 + i}`, { program: "7", program_name: "Malaria" })),
    connect("900", "Other", { program: "8", program_name: "Nutrition" }),
  ])
  const { user, onClose } = await openModal()

  await user.click(screen.getByTestId("create-source-tenant-100"))
  expect(screen.queryByTestId("create-workspace-connect-size-notice")).toBeNull()

  await user.click(screen.getByTestId("create-sources-select-shown"))
  expect(screen.getByTestId("create-workspace-connect-size-notice")).toHaveTextContent(
    "This workspace would have 26 CommCare Connect opportunities",
  )

  await user.click(screen.getByTestId("create-source-tenant-125"))
  expect(screen.queryByTestId("create-workspace-connect-size-notice")).toBeNull()
  await user.click(screen.getByTestId("create-source-tenant-125"))

  await user.click(screen.getByTestId("create-workspace-submit"))
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  expect(vi.mocked(workspaceApi.create).mock.calls[0][1]).toHaveLength(26)
})

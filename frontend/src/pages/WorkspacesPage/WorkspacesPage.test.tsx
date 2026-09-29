import { beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { WorkspacesPage } from "./WorkspacesPage"
import { useAppStore } from "@/store/store"
import type { TenantMembership } from "@/store/domainSlice"

vi.mock("@/api/workspaces", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/workspaces")>()
  return { ...actual, workspaceApi: { ...actual.workspaceApi, getMyInvites: vi.fn().mockResolvedValue([]) } }
})

const ws = (id: string, display_name: string, created_at: string): TenantMembership => ({
  id,
  name: display_name,
  display_name,
  is_auto_created: false,
  role: "manage",
  tenants: [],
  has_access: true,
  member_count: 1,
  schema_status: "available",
  last_synced_at: null,
  created_at,
})

function renderPage() {
  return render(
    <MemoryRouter>
      <WorkspacesPage />
    </MemoryRouter>,
  )
}

function rowIds() {
  return screen
    .getAllByTestId(/^workspace-row-/)
    .map((el) => el.getAttribute("data-testid")!.replace("workspace-row-", ""))
}

function many(n: number): TenantMembership[] {
  // Newest first, as the server returns them.
  return Array.from({ length: n }, (_, i) => {
    const seq = String(n - i).padStart(3, "0")
    const createdAt = new Date(Date.UTC(2026, 0, 1) + (n - i) * 60_000).toISOString()
    return ws(`w${seq}`, `Workspace ${seq}`, createdAt)
  })
}

describe("WorkspacesPage", () => {
  beforeEach(() => {
    useAppStore.setState({ domainsStatus: "loaded", domains: [] })
  })

  it("sorts newest first by default, and by name or oldest on request", async () => {
    // Deliberately not in server order, so the default must sort too.
    useAppStore.setState({
      domains: [
        ws("mid", "Alpha", "2026-02-01T00:00:00Z"),
        ws("new", "bravo", "2026-03-01T00:00:00Z"),
        ws("old", "Charlie", "2026-01-01T00:00:00Z"),
      ],
    })
    renderPage()
    const user = userEvent.setup()

    expect(rowIds()).toEqual(["new", "mid", "old"])

    await user.selectOptions(screen.getByTestId("workspaces-sort"), "name")
    expect(rowIds()).toEqual(["mid", "new", "old"])

    await user.selectOptions(screen.getByTestId("workspaces-sort"), "oldest")
    expect(rowIds()).toEqual(["old", "mid", "new"])
  })

  it("renders one page of rows and reveals more on request", async () => {
    useAppStore.setState({ domains: many(120) })
    renderPage()
    const user = userEvent.setup()

    expect(rowIds()).toHaveLength(50)
    expect(screen.getByTestId("workspaces-count")).toHaveTextContent("Showing 50 of 120")

    await user.click(screen.getByTestId("workspaces-show-more"))
    expect(rowIds()).toHaveLength(100)

    await user.click(screen.getByTestId("workspaces-show-more"))
    expect(rowIds()).toHaveLength(120)
    expect(screen.queryByTestId("workspaces-show-more")).toBeNull()
  })

  it("starts from one page again when the search changes", async () => {
    useAppStore.setState({ domains: many(120) })
    renderPage()
    const user = userEvent.setup()

    await user.click(screen.getByTestId("workspaces-show-more"))
    expect(rowIds()).toHaveLength(100)

    await user.type(screen.getByTestId("search-filter-input"), "Workspace")
    expect(rowIds()).toHaveLength(50)
  })

  it("starts from one page again when the sort changes", async () => {
    useAppStore.setState({ domains: many(120) })
    renderPage()
    const user = userEvent.setup()

    await user.click(screen.getByTestId("workspaces-show-more"))
    expect(rowIds()).toHaveLength(100)

    await user.selectOptions(screen.getByTestId("workspaces-sort"), "oldest")
    expect(rowIds()).toHaveLength(50)
    expect(rowIds()[0]).toBe("w001")
  })

  it("sorts names with numbers in natural order", async () => {
    useAppStore.setState({
      domains: [
        ws("ten", "Site 10", "2026-01-01T00:00:00Z"),
        ws("two", "Site 2", "2026-01-02T00:00:00Z"),
      ],
    })
    renderPage()
    await userEvent.setup().selectOptions(screen.getByTestId("workspaces-sort"), "name")
    expect(rowIds()).toEqual(["two", "ten"])
  })

  it("does not offer show more when everything fits", () => {
    useAppStore.setState({ domains: many(3) })
    renderPage()
    expect(rowIds()).toHaveLength(3)
    expect(screen.queryByTestId("workspaces-show-more")).toBeNull()
  })
})

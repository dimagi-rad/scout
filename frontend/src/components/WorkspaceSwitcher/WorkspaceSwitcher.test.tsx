import { act, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { api } from "@/api/client"
import type { WorkspaceListItem } from "@/api/workspaces"
import type { UserTenant } from "@/api/auth"
import { getUserTenantsCached } from "@/api/userTenantsCache"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal"
import { useAppStore } from "@/store/store"
import { WorkspaceSwitcher } from "./WorkspaceSwitcher"

vi.mock("@/components/CreateWorkspaceModal", () => ({ CreateWorkspaceModal: vi.fn(() => null) }))
vi.mock("@/api/userTenantsCache", () => ({ getUserTenantsCached: vi.fn() }))

function tenant(id: string, name: string, provider = "commcare_connect"): UserTenant {
  return {
    id: `membership-${id}`,
    tenant_uuid: `tenant-${id}`,
    provider,
    tenant_id: id,
    tenant_name: name,
    last_selected_at: null,
  }
}

const LAST_LOAD = "2026-04-16T12:00:00Z"
const AVAILABILITY_NOTE = "Current availability is checked when opening an artifact."

function workspace(overrides: Partial<WorkspaceListItem> = {}): WorkspaceListItem {
  return {
    id: "workspace-recorded",
    name: "recorded-load",
    display_name: "Recorded load",
    is_auto_created: false,
    role: "manage",
    tenants: [{ id: "tenant-1", tenant_name: "Example source", provider: "commcare" }],
    member_count: 1,
    schema_status: "available",
    last_synced_at: LAST_LOAD,
    created_at: "2026-01-01T12:00:00Z",
    ...overrides,
  }
}

function openSwitcher(workspaces: WorkspaceListItem[] = [workspace()]) {
  useAppStore.setState({ domains: workspaces, activeDomainId: workspaces[0]?.id ?? null })
  render(
    <MemoryRouter>
      <WorkspaceSwitcher />
    </MemoryRouter>,
  )
  fireEvent.click(screen.getByTestId("domain-selector"))
}

describe("workspace load-history indicator", () => {
  beforeEach(() => {
    localStorage.clear()
    useAppStore.setState(useAppStore.getInitialState(), true)
    vi.useFakeTimers({ toFake: ["Date"] })
    vi.setSystemTime(new Date("2026-09-14T12:00:00Z"))
    vi.spyOn(api, "get").mockRejectedValue(new Error("Indicator must not probe readiness"))
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it("does not claim query readiness from available schema metadata without any Cube status", () => {
    // This is also a valid list payload when there is no active semantic model
    // or Cube schema. See test_workspace_load_metadata.py for the API contract.
    openSwitcher()

    const indicator = screen.getByTestId("workspace-data-indicator-workspace-recorded")
    const label = `Last recorded import: ${new Date(LAST_LOAD).toLocaleDateString()}. ${AVAILABILITY_NOTE}`
    expect(indicator).toHaveAccessibleName(label)
    expect(indicator).toHaveAttribute("title", label)
    expect(indicator).toHaveAttribute("data-data-state", "recorded")
    expect(indicator).not.toHaveAccessibleName(/has data|synced|query ready/i)
    expect(indicator.querySelector(".bg-emerald-500")).toBeNull()
    expect(api.get).not.toHaveBeenCalled()
  })

  it.each([
    ["provisioning", "loading", "Importing data…", "svg.animate-spin"],
    ["failed", "failed", "Data setup failed.", "svg.text-destructive"],
    ["unavailable", "unavailable", "Data setup unavailable.", null],
  ] as const)("preserves %s setup status and the last recorded load", (status, state, prefix, icon) => {
    openSwitcher([workspace({ schema_status: status })])

    const indicator = screen.getByTestId("workspace-data-indicator-workspace-recorded")
    const label = `${prefix} Last recorded import: ${new Date(LAST_LOAD).toLocaleDateString()}. ${AVAILABILITY_NOTE}`
    expect(indicator).toHaveAttribute("data-data-state", state)
    expect(indicator).toHaveAccessibleName(label)
    expect(indicator).toHaveAttribute("title", label)
    if (icon) expect(indicator.querySelector(icon)).not.toBeNull()
    expect(indicator.querySelector(".bg-emerald-500")).toBeNull()
    expect(api.get).not.toHaveBeenCalled()
  })

  it.each([
    ["available", "unknown", ""],
    ["provisioning", "loading", "Importing data… "],
    ["failed", "failed", "Data setup failed. "],
    ["unavailable", "unavailable", "Data setup unavailable. "],
  ] as const)("does not invent a load time for %s metadata", (status, state, prefix) => {
    openSwitcher([workspace({ schema_status: status, last_synced_at: null })])

    const indicator = screen.getByTestId("workspace-data-indicator-workspace-recorded")
    const label = `${prefix}No import time recorded. ${AVAILABILITY_NOTE}`
    expect(indicator).toHaveAttribute("data-data-state", state)
    expect(indicator).toHaveAccessibleName(label)
    expect(indicator).toHaveAttribute("title", label)
    expect(indicator).not.toHaveAccessibleName(/has data|synced|query ready/i)
    expect(screen.getByTestId("domain-item-workspace-recorded")).toHaveAttribute("data-has-recorded-load", "false")
  })

  it("retains relative dates without describing a recent partial load as a full sync", () => {
    openSwitcher([workspace({ last_synced_at: "2026-09-14T11:48:00Z" })])

    expect(screen.getByTestId("workspace-data-indicator-workspace-recorded"))
      .toHaveAccessibleName(`Last recorded import: 12 minutes ago. ${AVAILABILITY_NOTE}`)
  })

  it("filters by recorded load history, including setup failures, without claiming current data", () => {
    openSwitcher([
      workspace(),
      workspace({ id: "failed-load", display_name: "Failed setup", schema_status: "failed" }),
      workspace({ id: "unavailable-load", display_name: "Unavailable setup", schema_status: "unavailable" }),
      workspace({
        id: "undated-schema",
        display_name: "No load time",
        last_synced_at: null,
        tenants: [{ id: "tenant-2", tenant_name: "Another source", provider: "ocs" }],
      }),
    ])

    const filter = screen.getByRole("button", { name: "Show only workspaces with recorded imports" })
    expect(filter).toHaveAttribute("aria-pressed", "false")
    expect(filter).toHaveAttribute("title", "Show only workspaces with recorded imports")
    fireEvent.click(filter)

    expect(filter).toHaveAttribute("aria-pressed", "true")
    expect(filter).toHaveAccessibleName("Showing only workspaces with recorded imports")
    expect(screen.queryByTestId("domain-item-undated-schema")).not.toBeInTheDocument()
    for (const id of ["workspace-recorded", "failed-load", "unavailable-load"]) {
      expect(screen.getByTestId(`domain-item-${id}`)).toHaveAttribute("data-has-recorded-load", "true")
    }

    fireEvent.change(screen.getByTestId("workspace-search"), { target: { value: "No load time" } })
    expect(screen.getByText("No recorded imports match these filters.")).toBeInTheDocument()
    fireEvent.click(filter)
    expect(screen.getByTestId("domain-item-undated-schema")).toBeInTheDocument()
    expect(api.get).not.toHaveBeenCalled()
  })
})

describe("provider tabs", () => {
  beforeEach(() => {
    localStorage.clear()
    useAppStore.setState(useAppStore.getInitialState(), true)
  })

  function providerWorkspace(id: string, provider: string, hasAccess = true): WorkspaceListItem {
    return workspace({
      id,
      display_name: `${provider} ${id}`,
      has_access: hasAccess,
      tenants: [{ id: `tenant-${id}`, tenant_name: `Source ${id}`, provider }],
    })
  }

  it("shows a tab for a provider whose workspaces are all no-access, and lists them there", () => {
    openSwitcher([
      providerWorkspace("cc1", "commcare"),
      providerWorkspace("cc2", "commcare"),
      providerWorkspace("ocs1", "ocs", false),
      providerWorkspace("ocs2", "ocs", false),
    ])

    const ocsTab = screen.getByTestId("workspace-seg-ocs")
    expect(ocsTab).toHaveTextContent("Open Chat Studio2")
    // Counts include no-access workspaces, so All is every workspace listed.
    expect(screen.getByTestId("workspace-seg-all")).toHaveTextContent("All4")
    expect(screen.getByTestId("workspace-seg-commcare")).toHaveTextContent("CommCare2")

    fireEvent.click(ocsTab)
    expect(screen.queryByTestId("domain-item-cc1")).not.toBeInTheDocument()
    expect(screen.getByTestId("domain-item-lost-ocs1")).toBeInTheDocument()
    expect(screen.getByTestId("domain-item-lost-ocs2")).toBeInTheDocument()

    fireEvent.click(screen.getByTestId("workspace-seg-commcare"))
    expect(screen.getByTestId("domain-item-cc1")).toBeInTheDocument()
    expect(screen.queryByTestId("workspace-list-lost")).not.toBeInTheDocument()
  })

  it("links the no-access section to Connected Accounts", () => {
    openSwitcher([providerWorkspace("cc1", "commcare"), providerWorkspace("ocs1", "ocs", false)])
    fireEvent.click(screen.getByTestId("workspace-seg-all"))

    expect(screen.getByTestId("workspace-lost-connections")).toHaveTextContent(
      "Check Connected Accounts",
    )
  })
})

describe("workspace sources", () => {
  beforeEach(() => {
    localStorage.clear()
    useAppStore.setState(useAppStore.getInitialState(), true)
  })

  const multiSource = workspace({
    id: "program",
    display_name: "CHC program",
    tenants: [
      { id: "t-a", tenant_name: "CHC Kenya", provider: "commcare_connect" },
      { id: "t-b", tenant_name: "CHC Uganda", provider: "commcare_connect" },
    ],
  })

  it("names every source of the active workspace on the trigger", () => {
    openSwitcher([multiSource])

    expect(screen.getByTestId("domain-selector")).toHaveAttribute(
      "title",
      "2 data sources: CHC Kenya, CHC Uganda",
    )
  })

  it("marks multi-source rows with their source count", () => {
    openSwitcher([workspace(), multiSource])
    fireEvent.click(screen.getByTestId("workspace-seg-all"))

    expect(screen.getByTestId("workspace-source-count-program")).toHaveTextContent("2 sources")
    expect(screen.queryByTestId("workspace-source-count-workspace-recorded")).toBeNull()
  })

  it("opens when another part of the app asks for it", () => {
    useAppStore.setState({ domains: [workspace()], activeDomainId: "workspace-recorded" })
    render(
      <MemoryRouter>
        <WorkspaceSwitcher />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId("workspace-search")).toBeNull()

    act(() => useAppStore.getState().uiActions.openWorkspaceSwitcher())

    expect(screen.getByTestId("workspace-search")).toBeInTheDocument()
  })

  it("keeps an open switcher's search on a repeat request", () => {
    openSwitcher()
    fireEvent.change(screen.getByTestId("workspace-search"), { target: { value: "rec" } })

    act(() => useAppStore.getState().uiActions.openWorkspaceSwitcher())

    expect(screen.getByTestId("workspace-search")).toHaveValue("rec")
  })

  it("ignores a request made before it mounted", () => {
    useAppStore.setState({ domains: [workspace()], activeDomainId: "workspace-recorded" })
    act(() => useAppStore.getState().uiActions.openWorkspaceSwitcher())

    render(
      <MemoryRouter>
        <WorkspaceSwitcher />
      </MemoryRouter>,
    )

    expect(screen.queryByTestId("workspace-search")).toBeNull()
    expect(useAppStore.getState().workspaceSwitcherMounts).toBe(1)
  })
})

describe("combining sources", () => {
  beforeEach(() => {
    localStorage.clear()
    useAppStore.setState(useAppStore.getInitialState(), true)
    useAppStore.setState({ user: { id: "user-1" } as never })
    vi.mocked(CreateWorkspaceModal).mockClear()
    vi.mocked(getUserTenantsCached).mockResolvedValue([
      tenant("k", "CHC Nutrition Kenya"),
      tenant("u", "CHC Nutrition Uganda"),
    ])
  })

  it("is a separate mode, so plain switching stays one click", async () => {
    openSwitcher()
    expect(screen.queryByTestId("workspace-basket-option-tenant-k")).toBeNull()

    fireEvent.click(screen.getByTestId("workspace-combine-toggle"))
    expect(screen.queryByTestId("workspace-list")).toBeNull()
    fireEvent.change(await screen.findByTestId("workspace-basket-search"), {
      target: { value: "CHC" },
    })
    fireEvent.click(await screen.findByTestId("workspace-basket-option-tenant-k"))
    fireEvent.click(screen.getByTestId("workspace-basket-option-tenant-u"))

    vi.useFakeTimers()
    fireEvent.click(screen.getByTestId("workspace-basket-combine"))
    await act(async () => vi.runAllTimers())
    vi.useRealTimers()

    expect(vi.mocked(CreateWorkspaceModal).mock.lastCall?.[0]).toMatchObject({
      initialName: "CHC Nutrition (2 sources)",
      initialSourceIds: ["tenant-k", "tenant-u"],
    })
  })

  it("leaves Enter on the toggle to the button instead of switching workspace", () => {
    openSwitcher([workspace({ id: "a", display_name: "A" }), workspace({ id: "b", display_name: "B" })])
    const before = useAppStore.getState().activeDomainId

    // jsdom does not turn Enter into a click; the browser does, which opens the panel.
    fireEvent.keyDown(screen.getByTestId("workspace-combine-toggle"), { key: "Enter" })

    expect(useAppStore.getState().activeDomainId).toBe(before)
  })

  it("still arrows through the list after a footer button takes focus", () => {
    const ws = [workspace({ id: "a", display_name: "A" }), workspace({ id: "b", display_name: "B" })]
    openSwitcher(ws)
    const toggle = screen.getByTestId("workspace-combine-toggle")

    fireEvent.keyDown(toggle, { key: "ArrowDown" })
    expect(screen.getByTestId("workspace-search")).toHaveFocus()
    fireEvent.keyDown(screen.getByTestId("workspace-search"), { key: "Enter" })

    expect(useAppStore.getState().activeDomainId).toBe("b")
  })

  it("does not switch workspace on Enter inside the panel, and Escape goes back", async () => {
    openSwitcher([workspace({ id: "a", display_name: "A" }), workspace({ id: "b", display_name: "B" })])
    const before = useAppStore.getState().activeDomainId
    fireEvent.click(screen.getByTestId("workspace-combine-toggle"))
    const search = await screen.findByTestId("workspace-basket-search")

    fireEvent.keyDown(search, { key: "Enter" })
    expect(useAppStore.getState().activeDomainId).toBe(before)

    fireEvent.keyDown(search, { key: "Escape" })
    expect(screen.getByTestId("workspace-list")).toBeInTheDocument()
  })

  it("goes back to the workspace list, keeping the ticks until the switcher closes", async () => {
    openSwitcher()
    fireEvent.click(screen.getByTestId("workspace-combine-toggle"))
    fireEvent.change(await screen.findByTestId("workspace-basket-search"), {
      target: { value: "Kenya" },
    })
    fireEvent.click(await screen.findByTestId("workspace-basket-option-tenant-k"))
    fireEvent.click(screen.getByTestId("workspace-combine-back"))

    expect(screen.getByTestId("workspace-list")).toBeInTheDocument()
    fireEvent.click(screen.getByTestId("workspace-combine-toggle"))
    expect(await screen.findByTestId("workspace-basket-chip-tenant-k")).toBeInTheDocument()
  })
})

describe("switcher at scale", () => {
  beforeEach(() => {
    localStorage.clear()
    useAppStore.setState(useAppStore.getInitialState(), true)
  })

  function many(count: number): WorkspaceListItem[] {
    return Array.from({ length: count }, (_, i) =>
      workspace({
        id: `auto-${i}`,
        display_name: `Auto ${String(i).padStart(4, "0")}`,
        is_auto_created: true,
      }),
    )
  }

  it("opens on recent and hand-made workspaces, not every auto-created one", () => {
    localStorage.setItem("scout.recentWorkspaces", JSON.stringify(["auto-7"]))
    const handMade = workspace({ id: "mine", display_name: "My program", is_auto_created: false })
    openSwitcher([handMade, ...many(1000)])

    const rows = screen
      .getByTestId("workspace-list")
      .querySelectorAll("[data-switcher-row]")
    expect([...rows].map((r) => r.getAttribute("data-testid"))).toEqual([
      "domain-item-auto-7",
      "domain-item-mine",
    ])
    expect(screen.getByText("Recent")).toBeInTheDocument()
    expect(screen.getByText("Your workspaces")).toBeInTheDocument()
    expect(screen.getByTestId("workspace-list-search-hint")).toHaveTextContent("1001")
  })

  it("arrows across the section headings to the right workspace", () => {
    localStorage.setItem("scout.recentWorkspaces", JSON.stringify(["auto-1"]))
    const handMade = workspace({ id: "mine", display_name: "My program", is_auto_created: false })
    openSwitcher([handMade, ...many(3)])
    const search = screen.getByTestId("workspace-search")

    fireEvent.keyDown(search, { key: "ArrowDown" })
    fireEvent.keyDown(search, { key: "Enter" })

    expect(useAppStore.getState().activeDomainId).toBe("mine")
  })

  it("previews only a few no-access workspaces in the default view", () => {
    const lost = Array.from({ length: 200 }, (_, i) =>
      workspace({ id: `lost-${i}`, display_name: `Lost ${i}`, has_access: false }),
    )
    localStorage.setItem("scout.recentWorkspaces", JSON.stringify(["auto-0"]))
    openSwitcher([...many(2), ...lost])

    expect(screen.getAllByTestId(/^domain-item-lost-/)).toHaveLength(5)
    expect(screen.getByTestId("workspace-list-lost-more")).toHaveTextContent("195 more")
  })

  it("picks the default view once a list that loads after opening arrives", () => {
    localStorage.setItem("scout.recentWorkspaces", JSON.stringify(["auto-1"]))
    openSwitcher([])

    act(() => useAppStore.setState({ domains: many(3) }))
    expect(screen.getByTestId("workspace-list").querySelectorAll("[data-switcher-row]")).toHaveLength(3)

    act(() => useAppStore.setState({ domains: many(100) }))
    expect(screen.getByTestId("workspace-seg-recent")).toHaveClass("bg-primary")
    expect(screen.getByTestId("workspace-list").querySelectorAll("[data-switcher-row]")).toHaveLength(1)
  })

  it("never renders more than 50 rows and asks for a narrower search", () => {
    openSwitcher(many(1000))
    fireEvent.change(screen.getByTestId("workspace-search"), { target: { value: "Auto" } })

    expect(screen.getByTestId("workspace-list").querySelectorAll("[data-switcher-row]")).toHaveLength(50)
    expect(screen.getByTestId("workspace-list-more")).toHaveTextContent(
      "950 more — refine your search",
    )
  })
})

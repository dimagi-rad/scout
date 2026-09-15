import { fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { api } from "@/api/client"
import type { WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { WorkspaceSwitcher } from "./WorkspaceSwitcher"

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
    const label = `Last recorded load: ${new Date(LAST_LOAD).toLocaleDateString()}. ${AVAILABILITY_NOTE}`
    expect(indicator).toHaveAccessibleName(label)
    expect(indicator).toHaveAttribute("title", label)
    expect(indicator).toHaveAttribute("data-data-state", "recorded")
    expect(indicator).not.toHaveAccessibleName(/has data|synced|query ready/i)
    expect(indicator.querySelector(".bg-emerald-500")).toBeNull()
    expect(api.get).not.toHaveBeenCalled()
  })

  it.each([
    ["provisioning", "loading", "Loading data…", "svg.animate-spin"],
    ["failed", "failed", "Data setup failed.", "svg.text-destructive"],
    ["unavailable", "unavailable", "Data setup unavailable.", null],
  ] as const)("preserves %s setup status and the last recorded load", (status, state, prefix, icon) => {
    openSwitcher([workspace({ schema_status: status })])

    const indicator = screen.getByTestId("workspace-data-indicator-workspace-recorded")
    const label = `${prefix} Last recorded load: ${new Date(LAST_LOAD).toLocaleDateString()}. ${AVAILABILITY_NOTE}`
    expect(indicator).toHaveAttribute("data-data-state", state)
    expect(indicator).toHaveAccessibleName(label)
    expect(indicator).toHaveAttribute("title", label)
    if (icon) expect(indicator.querySelector(icon)).not.toBeNull()
    expect(indicator.querySelector(".bg-emerald-500")).toBeNull()
    expect(api.get).not.toHaveBeenCalled()
  })

  it.each([
    ["available", "unknown", ""],
    ["provisioning", "loading", "Loading data… "],
    ["failed", "failed", "Data setup failed. "],
    ["unavailable", "unavailable", "Data setup unavailable. "],
  ] as const)("does not invent a load time for %s metadata", (status, state, prefix) => {
    openSwitcher([workspace({ schema_status: status, last_synced_at: null })])

    const indicator = screen.getByTestId("workspace-data-indicator-workspace-recorded")
    const label = `${prefix}No load time recorded. ${AVAILABILITY_NOTE}`
    expect(indicator).toHaveAttribute("data-data-state", state)
    expect(indicator).toHaveAccessibleName(label)
    expect(indicator).toHaveAttribute("title", label)
    expect(indicator).not.toHaveAccessibleName(/has data|synced|query ready/i)
    expect(screen.getByTestId("domain-item-workspace-recorded")).toHaveAttribute("data-has-recorded-load", "false")
  })

  it("retains relative dates without describing a recent partial load as a full sync", () => {
    openSwitcher([workspace({ last_synced_at: "2026-09-14T11:48:00Z" })])

    expect(screen.getByTestId("workspace-data-indicator-workspace-recorded"))
      .toHaveAccessibleName(`Last recorded load: 12 minutes ago. ${AVAILABILITY_NOTE}`)
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

    const filter = screen.getByRole("button", { name: "Show only workspaces with recorded loads" })
    expect(filter).toHaveAttribute("aria-pressed", "false")
    expect(filter).toHaveAttribute("title", "Show only workspaces with recorded loads")
    fireEvent.click(filter)

    expect(filter).toHaveAttribute("aria-pressed", "true")
    expect(filter).toHaveAccessibleName("Showing only workspaces with recorded loads")
    expect(screen.queryByTestId("domain-item-undated-schema")).not.toBeInTheDocument()
    for (const id of ["workspace-recorded", "failed-load", "unavailable-load"]) {
      expect(screen.getByTestId(`domain-item-${id}`)).toHaveAttribute("data-has-recorded-load", "true")
    }

    fireEvent.change(screen.getByTestId("workspace-search"), { target: { value: "No load time" } })
    expect(screen.getByText("No recorded loads match these filters.")).toBeInTheDocument()
    fireEvent.click(filter)
    expect(screen.getByTestId("domain-item-undated-schema")).toBeInTheDocument()
    expect(api.get).not.toHaveBeenCalled()
  })
})

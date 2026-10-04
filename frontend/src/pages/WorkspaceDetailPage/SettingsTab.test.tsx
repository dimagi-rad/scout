import { fireEvent, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { workspaceApi, type WorkspaceDetail } from "@/api/workspaces"
import { SettingsTab } from "./SettingsTab"

vi.mock("@/api/workspaces", () => ({
  workspaceApi: { update: vi.fn().mockResolvedValue({}), delete: vi.fn() },
}))

const workspace: WorkspaceDetail = {
  id: "ws-1",
  name: "Two sources",
  display_name: "Two sources",
  is_auto_created: false,
  role: "manage",
  system_prompt: "Answer in French.",
  schema_status: "available",
  tenant_count: 2,
  member_count: 1,
  last_synced_at: null,
  created_at: "2026-09-01T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
}

const redacted: Partial<WorkspaceDetail> = {
  system_prompt: "",
  missing_tenants: [
    {
      tenant_id: "t2",
      tenant_name: "Source Two",
      provider: "commcare",
      recovery: "reconnect",
      remedy: "reconnect CommCare in Connected Accounts",
    },
  ],
}

function renderTab(overrides: Partial<WorkspaceDetail> = {}) {
  render(
    <SettingsTab workspace={{ ...workspace, ...overrides }} onRename={vi.fn()} onDelete={vi.fn()} />,
  )
}

describe("workspace settings saves", () => {
  beforeEach(() => vi.mocked(workspaceApi.update).mockClear())

  it("does not send an unedited system prompt", async () => {
    renderTab()

    await userEvent.click(screen.getByTestId("settings-save-prompt"))

    expect(workspaceApi.update).not.toHaveBeenCalled()
  })

  it("does not send an unedited system prompt when the form is submitted directly", () => {
    renderTab()

    fireEvent.submit(screen.getByTestId("settings-system-prompt").closest("form")!)

    expect(workspaceApi.update).not.toHaveBeenCalled()
  })

  it("sends only the prompt once it is edited", async () => {
    renderTab()

    await userEvent.type(screen.getByTestId("settings-system-prompt"), " Be brief.")
    await userEvent.click(screen.getByTestId("settings-save-prompt"))

    expect(workspaceApi.update).toHaveBeenCalledExactlyOnceWith("ws-1", {
      system_prompt: "Answer in French. Be brief.",
    })
  })

  it("renames without sending the system prompt", async () => {
    renderTab()

    const name = screen.getByTestId("settings-name-input")
    await userEvent.clear(name)
    await userEvent.type(name, "Renamed")
    await userEvent.click(screen.getByTestId("settings-save-name"))

    expect(workspaceApi.update).toHaveBeenCalledExactlyOnceWith("ws-1", { name: "Renamed" })
  })

  it("offers no settings edits while the server has blanked the prompt for missing sources", () => {
    renderTab(redacted)

    expect(screen.getByTestId("settings-access-notice")).toHaveTextContent(
      "The workspace name and system prompt can't be changed",
    )
    expect(screen.getByTestId("settings-missing-t2")).toHaveTextContent(
      "Source Two: reconnect CommCare in Connected Accounts",
    )
    expect(screen.getByTestId("settings-system-prompt-unavailable")).toBeInTheDocument()
    expect(screen.queryByTestId("settings-system-prompt")).not.toBeInTheDocument()
    expect(screen.queryByTestId("settings-save-prompt")).not.toBeInTheDocument()
    // Every PATCH is refused in this state, so rename is not offered either.
    expect(screen.getByTestId("settings-name-input")).toBeDisabled()
    expect(screen.queryByTestId("settings-save-name")).not.toBeInTheDocument()
  })

  it("names sources that share a remedy on one line", () => {
    const sameRemedy = redacted.missing_tenants![0]
    renderTab({
      ...redacted,
      missing_tenants: [
        { ...sameRemedy, tenant_id: "t2", tenant_name: "Source Two" },
        { ...sameRemedy, tenant_id: "t3", tenant_name: "Source Three" },
      ],
    })

    expect(screen.getByTestId("settings-missing-t2")).toHaveTextContent(
      "Source Two, Source Three: reconnect CommCare in Connected Accounts",
    )
    expect(screen.queryByTestId("settings-missing-t3")).not.toBeInTheDocument()
  })

  it("tells a non-manager only that the prompt is hidden", () => {
    renderTab({ ...redacted, role: "read" })

    const notice = screen.getByTestId("settings-access-notice")
    expect(notice).toHaveTextContent("The system prompt is hidden until you regain access.")
    expect(notice).not.toHaveTextContent("can't be changed")
  })
})

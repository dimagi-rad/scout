import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { LostAccessModal } from "./LostAccessModal"
import { useAppStore } from "@/store/store"

const navigate = vi.fn()
vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => navigate,
}))

const ws = (id: string, has_access: boolean, provider = "commcare") => ({
  id,
  name: id,
  display_name: id,
  is_auto_created: false,
  role: "manage" as const,
  tenants: [{ id: `t-${id}`, tenant_name: id, provider }],
  has_access,
  member_count: 1,
  schema_status: "available" as const,
  last_synced_at: null,
  created_at: "2026-01-01T00:00:00Z",
})

function renderModal(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LostAccessModal />
    </MemoryRouter>,
  )
}

describe("LostAccessModal", () => {
  beforeEach(() => {
    navigate.mockClear()
    useAppStore.setState({ domainsStatus: "loaded", domains: [], activeDomainId: null })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("does not render while domains are still loading", () => {
    useAppStore.setState({ domainsStatus: "loading", domains: [ws("skelly", false)], activeDomainId: "skelly" })
    renderModal()
    expect(screen.queryByTestId("lost-access-modal")).toBeNull()
  })

  it("does not render when the active workspace still has access", () => {
    useAppStore.setState({ domains: [ws("live", true)], activeDomainId: "live" })
    renderModal()
    expect(screen.queryByTestId("lost-access-modal")).toBeNull()
  })

  it("gates and names the provider when the active workspace lost access", () => {
    useAppStore.setState({
      domains: [ws("skelly", false), ws("live", true)],
      activeDomainId: "skelly",
    })
    renderModal()

    expect(screen.getByTestId("lost-access-modal")).toBeInTheDocument()
    expect(screen.getByText(/lost access to “skelly”/)).toBeInTheDocument()
    expect(screen.getByText("CommCare")).toBeInTheDocument()
    // Only accessible workspaces appear in the picker.
    expect(screen.getByTestId("lost-access-goto-live")).toBeInTheDocument()
    expect(screen.queryByTestId("lost-access-goto-skelly")).toBeNull()
  })

  it("switches to a chosen accessible workspace", async () => {
    useAppStore.setState({
      domains: [ws("skelly", false), ws("live", true)],
      activeDomainId: "skelly",
    })
    renderModal()

    await userEvent.click(screen.getByTestId("lost-access-goto-live"))

    expect(useAppStore.getState().activeDomainId).toBe("live")
    expect(navigate).toHaveBeenCalledWith(expect.stringContaining("/live/chat"))
  })

  it("tells the user when they have no accessible workspaces", () => {
    useAppStore.setState({ domains: [ws("skelly", false)], activeDomainId: "skelly" })
    renderModal()

    expect(screen.getByTestId("lost-access-modal")).toBeInTheDocument()
    expect(screen.queryByTestId("lost-access-picker")).toBeNull()
    expect(screen.getByText(/don’t have access to any workspaces/)).toBeInTheDocument()
    expect(screen.getByText(/If you disconnected your account/)).toBeInTheDocument()
    expect(screen.getByText(/reconnecting alone won’t restore those permissions/)).toBeInTheDocument()
  })

  it("lets a disconnected user reach connection management without another workspace", async () => {
    useAppStore.setState({ domains: [ws("skelly", false)], activeDomainId: "skelly" })
    renderModal()
    await userEvent.click(screen.getByRole("button", { name: "Open Connected Accounts" }))
    expect(navigate).toHaveBeenCalledWith("/settings/connections")
  })

  it.each(["/settings/connections", "/settings/connections/"])("does not cover the recovery page %s", (path) => {
    useAppStore.setState({ domains: [ws("skelly", false)], activeDomainId: "skelly" })
    renderModal(path)
    expect(screen.queryByTestId("lost-access-modal")).not.toBeInTheDocument()
  })

  it("links to the workspace's own page, where the user can leave or remove a source", async () => {
    useAppStore.setState({ domains: [ws("skelly", false)], activeDomainId: "skelly" })
    renderModal()

    await userEvent.click(screen.getByTestId("lost-access-workspace-settings"))

    expect(navigate).toHaveBeenCalledWith(expect.stringMatching(/\/workspaces\/.*skelly$/))
  })

  it("does not cover the active workspace's own page", () => {
    useAppStore.setState({ domains: [ws("skelly", false)], activeDomainId: "skelly" })
    render(
      <MemoryRouter initialEntries={["/workspaces/skelly/skelly"]}>
        <LostAccessModal />
      </MemoryRouter>,
    )

    expect(screen.queryByTestId("lost-access-modal")).toBeNull()
  })

  it("offers Connected Accounts even without a missing-source list", async () => {
    useAppStore.setState({ domains: [ws("skelly", false)], activeDomainId: "skelly" })
    renderModal()

    await userEvent.click(screen.getByTestId("lost-access-connections"))

    expect(navigate).toHaveBeenCalledWith("/settings/connections")
  })
})

describe("LostAccessModal upstream recheck", () => {
  beforeEach(() => {
    navigate.mockClear()
    useAppStore.setState({ domainsStatus: "loaded", domains: [], activeDomainId: null })
  })

  it("offers an upstream recheck from inside the gate", async () => {
    const retry = vi.fn().mockResolvedValue(undefined)
    useAppStore.setState({
      domains: [ws("skelly", false)],
      activeDomainId: "skelly",
      uiActions: { ...useAppStore.getState().uiActions, retryAccessVerification: retry },
    })
    renderModal()

    await userEvent.click(screen.getByTestId("lost-access-retry-verification"))

    expect(retry).toHaveBeenCalledWith("skelly")
  })

  it("shows the retry outcome inside the gate", () => {
    useAppStore.setState({ domains: [ws("skelly", false)], activeDomainId: "skelly" })
    // Set after the switch: selecting a workspace resets the thread-denial state.
    useAppStore.setState({ threadsAccessLostMessage: "We couldn't verify your access right now." })
    renderModal()

    expect(screen.getByTestId("lost-access-retry-outcome")).toHaveTextContent("couldn't verify")
  })
})

describe("LostAccessModal with missing sources", () => {
  const partial = {
    ...ws("both", false, "ocs"),
    missing_tenants: [
      {
        tenant_id: "t-bot-b",
        tenant_name: "Bot B",
        provider: "ocs",
        recovery: "connect_team" as const,
        team_slug: "team-b",
        team_name: "Team B",
        remedy: "connect Open Chat Studio team 'Team B' in Connected Accounts",
      },
    ],
  }

  beforeEach(() => {
    navigate.mockClear()
    useAppStore.setState({ domainsStatus: "loaded", domains: [partial], activeDomainId: "both" })
  })

  it("names each missing source with its remedy", () => {
    renderModal()

    expect(screen.getByText(/can’t open “both” yet/)).toBeInTheDocument()
    expect(screen.getByTestId("lost-access-missing-t-bot-b")).toHaveTextContent(
      "Bot B: connect Open Chat Studio team 'Team B' in Connected Accounts",
    )
    expect(screen.queryByText(/If you disconnected your account/)).not.toBeInTheDocument()
  })

  it("links to Connected Accounts", async () => {
    renderModal()

    await userEvent.click(screen.getByTestId("lost-access-connections"))

    expect(navigate).toHaveBeenCalledWith("/settings/connections")
  })

  it.each(["/settings/connections", "/settings/connections/"])(
    "does not cover Connected Accounts (%s), where the user fixes it",
    (path) => {
      render(
        <MemoryRouter initialEntries={[path]}>
          <LostAccessModal />
        </MemoryRouter>,
      )

      expect(screen.queryByTestId("lost-access-modal")).toBeNull()
    },
  )
})

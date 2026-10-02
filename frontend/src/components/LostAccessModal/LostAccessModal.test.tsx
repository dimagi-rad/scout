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

  it("titles the gate with the workspace name, not the source-count display name", () => {
    useAppStore.setState({
      domains: [{ ...ws("skelly", false), name: "ocs demo 2", display_name: "ocs demo 2 · 3 sources" }],
      activeDomainId: "skelly",
    })
    renderModal()

    expect(screen.getByRole("heading")).toHaveTextContent("You’ve lost access to “ocs demo 2”")
  })

  it("points a workspace with no sources at deleting it, not at reconnecting (#381)", () => {
    useAppStore.setState({
      domains: [{ ...ws("empty", false), tenants: [] }, ws("live", true)],
      activeDomainId: "empty",
    })
    renderModal()

    expect(screen.getByText(/“empty” has no data sources/)).toBeInTheDocument()
    expect(screen.getByTestId("lost-access-no-sources")).toHaveTextContent("A manager can delete it")
    expect(screen.getByTestId("lost-access-workspace-settings")).toHaveTextContent("Leave or delete")
    expect(screen.queryByTestId("lost-access-connections")).toBeNull()
    expect(screen.queryByTestId("lost-access-retry-verification")).toBeNull()
    expect(screen.queryByText(/reconnect it in Connected Accounts/)).toBeNull()
  })

  it("lists accessible workspaces alphabetically by display name", () => {
    useAppStore.setState({
      domains: [ws("skelly", false), ws("zulu", true), ws("bravo", true), ws("Alpha", true)],
      activeDomainId: "skelly",
    })
    renderModal()

    const order = [...screen.getByTestId("lost-access-picker").querySelectorAll("button")].map(
      (el) => el.getAttribute("data-testid"),
    )
    expect(order).toEqual(["lost-access-goto-Alpha", "lost-access-goto-bravo", "lost-access-goto-zulu"])
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
    useAppStore.setState({ accessRetryOutcome: "We couldn't verify your access right now." })
    renderModal()

    expect(screen.getByTestId("lost-access-retry-outcome")).toHaveTextContent("couldn't verify")
  })

  it("does not repeat the threads denial as a retry outcome", () => {
    useAppStore.setState({ domains: [ws("skelly", false)], activeDomainId: "skelly" })
    useAppStore.setState({ threadsStatus: "error", threadsAccessDenialReason: "tenant_access_lost" })
    renderModal()

    expect(screen.queryByTestId("lost-access-retry-outcome")).toBeNull()
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

  it("names sources that share a remedy on one line", () => {
    const ended =
      "your access through Open Chat Studio ended: reconnect it in Connected Accounts"
    const lost = (id: string, name: string) => ({
      tenant_id: id,
      tenant_name: name,
      provider: "ocs",
      recovery: "access_removed" as const,
      remedy: ended,
    })
    useAppStore.setState({
      domains: [{ ...partial, missing_tenants: [lost("a", "A"), lost("b", "B"), lost("c", "C")] }],
    })
    renderModal()

    const items = screen.getByTestId("lost-access-missing").querySelectorAll("li")
    expect(items).toHaveLength(1)
    expect(items[0]).toHaveTextContent(`A, B, C: ${ended}`)
  })

  it("names a blank-named source by its provider", () => {
    const tenant = { ...partial.missing_tenants[0], tenant_id: "t-blank", tenant_name: "" }
    useAppStore.setState({ domains: [{ ...partial, missing_tenants: [tenant] }] })
    renderModal()

    expect(screen.getByTestId("lost-access-missing-t-blank")).toHaveTextContent(
      /^Open Chat Studio: connect/,
    )
  })

  it("confirms a denied retry without repeating the source list", () => {
    useAppStore.setState({
      threadsAccessDenialReason: "tenant_access_lost",
      accessRetryOutcome: "Still needed — 'Bot B': connect …",
    })
    renderModal()

    const outcome = screen.getByTestId("lost-access-retry-outcome")
    expect(outcome).toHaveTextContent("sources above are still needed")
    expect(outcome).not.toHaveTextContent("Bot B")
  })

  it("shows a retry that could not verify, even with sources listed", () => {
    useAppStore.setState({
      threadsAccessDenialReason: "verification_unavailable",
      accessRetryOutcome: "We couldn't verify your access right now.",
    })
    renderModal()

    expect(screen.getByTestId("lost-access-retry-outcome")).toHaveTextContent("couldn't verify")
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

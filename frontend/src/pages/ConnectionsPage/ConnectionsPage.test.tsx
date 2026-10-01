import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { refreshUserTenants } from "@/api/userTenantsCache"
import { useAppStore } from "@/store/store"
import { ConnectionsPage } from "./ConnectionsPage"

vi.mock("@/api/client", () => ({ api: { get: vi.fn() } }))
vi.mock("@/api/userTenantsCache", () => ({ refreshUserTenants: vi.fn() }))

describe("ConnectionsPage", () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(() => useAppStore.setState({ user: null }))

  it("refreshes sources upstream and reloads connections without disconnecting", async () => {
    vi.mocked(api.get).mockImplementation((path) =>
      Promise.resolve(path === "/api/auth/providers/" ? { providers: [] } : []),
    )
    vi.mocked(refreshUserTenants).mockResolvedValue([])
    useAppStore.setState({
      user: { id: "u1", email: "u@example.com", name: "U", is_staff: false, onboarding_complete: true },
    })
    render(<ConnectionsPage />)
    const button = await screen.findByTestId("refresh-sources-button")
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/auth/connections/"))
    vi.mocked(api.get).mockClear()

    await act(async () => { button.click() })

    expect(refreshUserTenants).toHaveBeenCalledExactlyOnceWith("u1")
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/auth/connections/"))
  })

  it("waits for provider refresh before reading connection health", async () => {
    let completeRefresh!: (value: { providers: [] }) => void
    const refreshed = new Promise<{ providers: [] }>((resolve) => { completeRefresh = resolve })
    vi.mocked(api.get).mockImplementation((path) => {
      if (path === "/api/auth/providers/") return refreshed
      return Promise.resolve([])
    })
    render(<ConnectionsPage />)
    expect(api.get).toHaveBeenCalledWith("/api/auth/providers/")
    expect(api.get).not.toHaveBeenCalledWith("/api/auth/connections/")
    await act(async () => { completeRefresh({ providers: [] }) })
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/auth/connections/"))
  })

  it("tells a team-less OCS sign-in to connect a team, not that it expired", async () => {
    vi.mocked(api.get).mockImplementation((path) => {
      if (path === "/api/auth/providers/") {
        return Promise.resolve({
          providers: [
            {
              id: "ocs",
              name: "Open Chat Studio",
              login_url: "/accounts/ocs/login/",
              connected: true,
              status: "needs_team",
              supports_multiple_scopes: true,
            },
          ],
        })
      }
      return Promise.resolve([
        {
          connection_id: "c1",
          provider: "ocs",
          credential_type: "oauth",
          scope_key: "",
          scope_label: "",
          status: "needs_team",
          chatbots: [],
        },
      ])
    })
    render(<ConnectionsPage />)

    expect(await screen.findByTestId("connection-needs-team-c1")).toBeTruthy()
    expect(screen.getByText("No team selected")).toBeTruthy()
    expect(screen.getByTestId("connect-ocs").textContent).toBe("Connect a team")
    expect(screen.queryByText("Connection expired")).toBeNull()
    expect(screen.getByTestId("remove-connection-c1")).toBeTruthy()
    expect(screen.getByTestId("disconnect-ocs")).toBeTruthy()
  })

  it.each([
    ["unavailable", "Connected, but we couldn't check right now. Try again later.", false, true],
    ["expired", "Connection expired", true, false],
  ])("renders a %s provider with the right Reconnect affordance", async (status, label, reconnect, disconnect) => {
    vi.mocked(api.get).mockImplementation((path) =>
      Promise.resolve(
        path === "/api/auth/providers/"
          ? {
              providers: [
                {
                  id: "commcare",
                  name: "CommCare HQ",
                  login_url: "/accounts/commcare/login/",
                  connected: true,
                  status,
                },
              ],
            }
          : [],
      ),
    )
    render(<ConnectionsPage />)

    expect((await screen.findByTestId("provider-status-commcare")).textContent).toBe(label)
    expect(screen.queryByTestId("connect-commcare") !== null).toBe(reconnect)
    expect(screen.queryByText("Reconnect") !== null).toBe(reconnect)
    expect(screen.queryByTestId("disconnect-commcare") !== null).toBe(disconnect)
  })

  it("still offers connecting another team while a scoped provider is unavailable", async () => {
    vi.mocked(api.get).mockImplementation((path) =>
      Promise.resolve(
        path === "/api/auth/providers/"
          ? {
              providers: [
                {
                  id: "ocs",
                  name: "Open Chat Studio",
                  login_url: "/accounts/ocs/login/",
                  connected: true,
                  status: "unavailable",
                  supports_multiple_scopes: true,
                },
              ],
            }
          : [],
      ),
    )
    render(<ConnectionsPage />)

    expect(await screen.findByTestId("connect-another-ocs")).toBeTruthy()
    expect(screen.queryByTestId("connect-ocs")).toBeNull()
  })

  it("marks a CommCare connection on the EU server", async () => {
    vi.mocked(api.get).mockImplementation((path) =>
      Promise.resolve(
        path === "/api/auth/providers/"
          ? { providers: [] }
          : [
              {
                connection_id: "eu1",
                provider: "commcare",
                credential_type: "api_key",
                scope_key: "eu",
                scope_label: "EU",
                status: null,
                chatbots: [],
              },
              {
                connection_id: "www1",
                provider: "commcare",
                credential_type: "api_key",
                scope_key: "",
                scope_label: "",
                status: null,
                chatbots: [],
              },
            ],
      ),
    )
    render(<ConnectionsPage />)

    expect((await screen.findByTestId("connection-team-eu1")).textContent).toBe("CommCare HQ (EU)")
    expect(screen.getByTestId("connection-team-www1").textContent).toBe("commcare")
  })

  describe("filtering", () => {
    const chatbot = (id: string, name: string) => ({
      membership_id: id,
      tenant_id: `ext-${id}`,
      tenant_name: name,
      team_slug: "",
      team_name: "",
    })
    const conn = (id: string, provider: string, scope_label: string, chatbots: unknown[]) => ({
      connection_id: id,
      provider,
      credential_type: "oauth",
      scope_key: "",
      scope_label,
      status: "connected",
      chatbots,
    })

    async function renderWithConnections() {
      vi.mocked(api.get).mockImplementation((path) =>
        Promise.resolve(
          path === "/api/auth/providers/"
            ? { providers: [] }
            : [
                conn("c1", "ocs", "Acme Health Team", [chatbot("m1", "Triage Bot")]),
                conn("c2", "commcare", "Other Org", [chatbot("m2", "Survey")]),
                conn("c3", "ocs", "Empty Team", []),
              ],
        ),
      )
      render(<ConnectionsPage />)
      await screen.findByTestId("connection-card-c1")
    }

    it("searches by the team name shown on the card", async () => {
      await renderWithConnections()
      fireEvent.change(screen.getByTestId("search-filter-input"), {
        target: { value: "acme" },
      })
      expect(screen.getByTestId("connection-card-c1")).toBeTruthy()
      expect(screen.queryByTestId("connection-card-c2")).toBeNull()
      expect(screen.queryByTestId("connection-card-c3")).toBeNull()
    })

    it("finds a connection that has no chatbots", async () => {
      await renderWithConnections()
      fireEvent.change(screen.getByTestId("search-filter-input"), {
        target: { value: "empty team" },
      })
      expect(screen.getByTestId("connection-card-c3")).toBeTruthy()
      expect(screen.queryByTestId("connection-card-c1")).toBeNull()
    })

    it("still searches by chatbot name", async () => {
      await renderWithConnections()
      fireEvent.change(screen.getByTestId("search-filter-input"), {
        target: { value: "survey" },
      })
      expect(screen.getByTestId("connection-card-c2")).toBeTruthy()
      expect(screen.queryByTestId("connection-card-c1")).toBeNull()
    })

    it("filters by provider and combines with search", async () => {
      await renderWithConnections()
      fireEvent.click(screen.getByTestId("filter-provider-ocs"))
      expect(screen.queryByTestId("connection-card-c2")).toBeNull()
      expect(screen.getByTestId("connection-card-c1")).toBeTruthy()
      fireEvent.change(screen.getByTestId("search-filter-input"), {
        target: { value: "empty" },
      })
      expect(screen.queryByTestId("connection-card-c1")).toBeNull()
      expect(screen.getByTestId("connection-card-c3")).toBeTruthy()
    })
  })
})

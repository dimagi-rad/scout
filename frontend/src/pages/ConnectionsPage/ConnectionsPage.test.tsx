import { act, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { refreshUserTenants } from "@/api/userTenantsCache"
import { useAppStore } from "@/store/store"
import { ConnectionsPage } from "./ConnectionsPage"

vi.mock("@/api/client", () => ({ api: { get: vi.fn() } }))
vi.mock("@/api/userTenantsCache", () => ({ refreshUserTenants: vi.fn() }))

describe("ConnectionsPage", () => {
  beforeEach(() => vi.clearAllMocks())

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
})

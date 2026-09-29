import { act, render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { ConnectionsPage } from "./ConnectionsPage"

vi.mock("@/api/client", () => ({ api: { get: vi.fn() } }))

describe("ConnectionsPage", () => {
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

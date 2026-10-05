import { act, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { postOAuthStart, type OAuthProvider } from "@/lib/oauth"
import {
  CHAIN_CONTINUE_DELAY_MS,
  OcsTeamsPanel,
  PENDING_RECHECK_MS,
  type OcsTeamsState,
} from "./OcsTeamsPanel"

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  getCsrfToken: () => "tok",
}))
vi.mock("@/lib/oauth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/oauth")>()),
  postOAuthStart: vi.fn(() => Promise.resolve()),
}))

const provider: OAuthProvider = {
  id: "ocs",
  name: "Open Chat Studio",
  login_url: "/accounts/ocs/login/",
  connected: true,
  status: "connected",
  supports_multiple_scopes: true,
}

const teams = [
  { slug: "alpha", name: "Alpha", connected: true },
  { slug: "beta", name: "Beta", connected: false },
  { slug: "gamma", name: "Gamma", connected: false },
]

function serve(state: OcsTeamsState) {
  vi.mocked(api.get).mockResolvedValue(state)
}

async function renderPanel() {
  render(<OcsTeamsPanel provider={provider} />)
  await act(async () => {})
}

describe("OcsTeamsPanel", () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(() => vi.useRealTimers())

  it("asks for one reconnect when the team list is unknown", async () => {
    serve({ available: true, known: false, teams: [], flow: null, next: null })
    await renderPanel()
    expect(screen.getByTestId("ocs-teams-hint")).toBeInTheDocument()
    expect(screen.queryByTestId("ocs-teams-connect-all")).not.toBeInTheDocument()
  })

  it("renders nothing for a response it can't read", async () => {
    vi.mocked(api.get).mockResolvedValue([])
    const { container } = render(<OcsTeamsPanel provider={provider} />)
    await act(async () => {})
    expect(container).toBeEmptyDOMElement()
  })

  it("offers each unconnected team pinned by slug", async () => {
    serve({ available: true, known: true, teams, flow: null, next: null })
    await renderPanel()
    expect(screen.queryByTestId("ocs-team-alpha")).not.toBeInTheDocument()
    const link = screen.getByTestId("ocs-team-connect-beta")
    expect(link.getAttribute("href")).toContain("process=connect")
    expect(link.getAttribute("href")).toContain("team=beta")
    expect(screen.getByTestId("ocs-teams-connect-all")).toHaveTextContent(
      "Connect all remaining teams (2)",
    )
  })

  it("starts the chain, then continues to the first team after a pause", async () => {
    serve({ available: true, known: true, teams, flow: null, next: null })
    await renderPanel()
    vi.useFakeTimers()
    vi.mocked(api.post).mockResolvedValue({
      available: true,
      known: true,
      teams,
      flow: {
        mode: "all",
        connected: [],
        remaining: teams.slice(1),
        pending: null,
        finished: false,
        stopped: null,
      },
      next: "beta",
    })

    await act(async () => {
      fireEvent.click(screen.getByTestId("ocs-teams-connect-all"))
    })
    expect(api.post).toHaveBeenCalledWith("/api/auth/ocs/teams/connect-all/")
    expect(screen.getByTestId("ocs-teams-flow-next")).toHaveTextContent("Connecting Beta")
    expect(postOAuthStart).not.toHaveBeenCalled()

    await act(async () => {
      vi.advanceTimersByTime(CHAIN_CONTINUE_DELAY_MS)
    })
    expect(postOAuthStart).toHaveBeenCalledOnce()
    expect(vi.mocked(postOAuthStart).mock.calls[0][0]).toContain("team=beta")
  })

  it("stops the chain before the next hop", async () => {
    vi.useFakeTimers()
    const running = {
      mode: "all" as const,
      connected: [{ slug: "beta", name: "Beta" }],
      remaining: [{ slug: "gamma", name: "Gamma" }],
      pending: null,
      finished: false,
      stopped: null,
    }
    serve({ available: true, known: true, teams, flow: running, next: "gamma" })
    await renderPanel()
    vi.mocked(api.post).mockResolvedValue({
      available: true,
      known: true,
      teams,
      flow: {
        ...running,
        stopped: { reason: "user", team: { slug: "gamma", name: "Gamma" }, got: null },
      },
      next: null,
    })

    await act(async () => {
      fireEvent.click(screen.getByTestId("ocs-teams-stop"))
    })
    await act(async () => {
      vi.advanceTimersByTime(CHAIN_CONTINUE_DELAY_MS * 2)
    })

    expect(api.post).toHaveBeenCalledWith("/api/auth/ocs/teams/stop/")
    expect(postOAuthStart).not.toHaveBeenCalled()
    expect(screen.getByTestId("ocs-teams-flow-stopped")).toHaveTextContent('Stopped before "Gamma"')
    expect(screen.getByTestId("ocs-teams-connect-all")).toHaveTextContent("Resume")
  })

  it("explains a team OCS swapped and does not continue", async () => {
    vi.useFakeTimers()
    serve({
      available: true,
      known: true,
      teams,
      flow: {
        mode: "all",
        connected: [],
        remaining: teams.slice(1),
        pending: null,
        finished: false,
        stopped: {
          reason: "mismatch",
          team: { slug: "beta", name: "Beta" },
          got: { slug: "alpha", name: "Alpha" },
        },
      },
      next: null,
    })
    await renderPanel()
    await act(async () => {
      vi.advanceTimersByTime(CHAIN_CONTINUE_DELAY_MS * 2)
    })

    expect(screen.getByTestId("ocs-teams-flow-stopped")).toHaveTextContent(
      'returned team "Alpha" instead of "Beta"',
    )
    expect(postOAuthStart).not.toHaveBeenCalled()
  })

  it("stops the chain when a hop can't start", async () => {
    vi.useFakeTimers()
    const running = {
      mode: "all" as const,
      connected: [],
      remaining: teams.slice(1),
      pending: null,
      finished: false,
      stopped: null,
    }
    serve({ available: true, known: true, teams, flow: running, next: "beta" })
    await renderPanel()
    vi.mocked(postOAuthStart).mockRejectedValueOnce(new Error("offline"))
    vi.mocked(api.post).mockResolvedValue({
      available: true,
      known: true,
      teams,
      flow: {
        ...running,
        stopped: { reason: "user", team: { slug: "beta", name: "Beta" }, got: null },
      },
      next: null,
    })

    await act(async () => {
      vi.advanceTimersByTime(CHAIN_CONTINUE_DELAY_MS)
    })

    expect(api.post).toHaveBeenCalledWith("/api/auth/ocs/teams/stop/", { reason: "failed" })
    expect(screen.getByTestId("ocs-teams-error")).toBeInTheDocument()
    expect(screen.getByTestId("ocs-teams-connect-all")).toHaveTextContent("Resume")
  })

  it("cancels the hop locally the moment Stop is pressed", async () => {
    vi.useFakeTimers()
    const running = {
      mode: "all" as const,
      connected: [],
      remaining: teams.slice(1),
      pending: null,
      finished: false,
      stopped: null,
    }
    serve({ available: true, known: true, teams, flow: running, next: "beta" })
    await renderPanel()
    vi.mocked(api.post).mockReturnValue(new Promise(() => {}))

    await act(async () => {
      vi.advanceTimersByTime(CHAIN_CONTINUE_DELAY_MS - 300)
      fireEvent.click(screen.getByTestId("ocs-teams-stop"))
    })
    await act(async () => {
      vi.advanceTimersByTime(CHAIN_CONTINUE_DELAY_MS)
    })

    expect(postOAuthStart).not.toHaveBeenCalled()
  })

  it("keeps a running chain's Stop when a reload fails", async () => {
    vi.useFakeTimers()
    serve({
      available: true,
      known: true,
      teams,
      flow: {
        mode: "all",
        connected: [],
        remaining: teams.slice(1),
        pending: { slug: "beta", name: "Beta" },
        finished: false,
        stopped: null,
      },
      next: null,
    })
    await renderPanel()
    expect(screen.getByTestId("ocs-teams-flow-pending")).toBeInTheDocument()
    vi.mocked(api.get).mockRejectedValue(new Error("503"))

    await act(async () => {
      vi.advanceTimersByTime(PENDING_RECHECK_MS)
    })

    expect(screen.getByTestId("ocs-teams-error")).toBeInTheDocument()
    expect(screen.getByTestId("ocs-teams-stop")).toBeInTheDocument()

    vi.mocked(api.get).mockClear()
    await act(async () => {
      vi.advanceTimersByTime(PENDING_RECHECK_MS)
    })
    expect(api.get).toHaveBeenCalledWith("/api/auth/ocs/teams/")
  })

  it("ignores a malformed flow instead of breaking the page", async () => {
    serve({ available: true, known: true, teams, flow: { mode: "all" }, next: null } as unknown as OcsTeamsState)
    await renderPanel()
    expect(screen.queryByTestId("ocs-teams-flow")).not.toBeInTheDocument()
    expect(screen.getByTestId("ocs-team-beta")).toBeInTheDocument()
  })

  it("hides the reconnect hint when Scout doesn't request the teams scope", async () => {
    vi.mocked(api.get).mockResolvedValue({
      available: false,
      known: false,
      teams: [],
      flow: null,
      next: null,
    })
    const { container } = render(<OcsTeamsPanel provider={provider} />)
    await act(async () => {})
    expect(container).toBeEmptyDOMElement()
  })

  it("keeps a stored team list usable when the teams scope is off", async () => {
    serve({ available: false, known: true, teams, flow: null, next: null })
    await renderPanel()
    expect(screen.getByTestId("ocs-team-connect-beta").getAttribute("href")).toContain("team=beta")
    expect(screen.getByTestId("ocs-teams-connect-all")).toBeInTheDocument()
  })
})

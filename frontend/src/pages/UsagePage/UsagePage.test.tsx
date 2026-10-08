import { beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { ApiError } from "@/api/client"
import { usageApi, type UsageDashboard } from "@/api/usage"
import { formatBytes, formatCompact, formatMs } from "./format"
import { useAppStore } from "@/store/store"
import { UsagePage } from "./UsagePage"

vi.mock("@/api/usage", () => ({ usageApi: { dashboard: vi.fn() } }))

const mocked = vi.mocked(usageApi)
const days = ["2026-10-07", "2026-10-08"]
const pair = { p50: 1200, p95: 4000 }

const dashboard: UsageDashboard = {
  window: { start: "2026-10-07T00:00:00Z", end: "2026-10-08T12:00:00Z", days: 2 },
  days,
  active_users: { daily: [2, 3], dau: 3, wau: 4, mau: 5 },
  features: {
    artifact_views: [1, 2],
    recipe_runs: [0, 1],
    workspace_switches: [0, 0],
    logins: [1, 1],
    turns: [4, 6],
  },
  created: { threads: [1, 1], artifacts: [0, 2], tenants: [0, 0], workspaces: [0, 1] },
  updated: { threads: [1, null], artifacts: [0, null], tenants: [0, null], workspaces: [0, null] },
  turns: {
    total: 10,
    background: 2,
    outcomes: { completed: 8, stopped: 1, failed: 1 },
    duration_ms: pair,
    ttft_ms: { p50: 800, p95: 2000 },
    tool_calls_per_turn: 1.5,
    tokens: { input: 12000, output: 900, cache_read: 8000 },
    daily: [
      { duration_p50_ms: 1000, duration_p95_ms: 3000, ttft_p50_ms: 700 },
      { duration_p50_ms: 1300, duration_p95_ms: 4200, ttft_p50_ms: 900 },
    ],
  },
  tools: [{ name: "query", calls: 20, errors: 2, error_rate: 0.1, p50_ms: 300, p95_ms: 2500 }],
  tokens_by_workspace: [
    { workspace_id: "w1", name: "Kisumu", input_tokens: 12000, output_tokens: 900, runs: 10 },
  ],
  loads: {
    total: 3,
    failed: 1,
    duration_ms: pair,
    phases: [{ phase: "building_tables", p50_ms: 60000, p95_ms: 90000 }],
  },
  materializations: { total: 4, states: { completed: 3, failed: 1 }, duration_ms: pair },
  recipe_runs: { total: 2, failed: 1, duration_ms: pair },
  schema_sizes: {
    as_of: "2026-10-07",
    retained_bytes: 1024,
    total_daily: [2048, 4096],
    top_tenants: [{ tenant_id: "t1", name: "Demo domain", bytes: 4096 }],
  },
}

const viewer = { id: "1", email: "a@b.c", name: "A", is_staff: true, onboarding_complete: true }

describe("Usage page", () => {
  beforeEach(() => {
    vi.resetAllMocks()
    useAppStore.setState({ user: { ...viewer, can_view_usage_dashboard: true } })
  })

  it("never asks the server when the user lacks the permission", () => {
    useAppStore.setState({ user: { ...viewer, can_view_usage_dashboard: false } })
    render(<UsagePage />)

    expect(screen.getByTestId("usage-forbidden")).toBeInTheDocument()
    expect(mocked.dashboard).not.toHaveBeenCalled()
  })

  it("shows the headline numbers and tables", async () => {
    mocked.dashboard.mockResolvedValue(dashboard)
    render(<UsagePage />)

    expect(await screen.findByTestId("usage-tile-dau")).toHaveTextContent("3")
    expect(screen.getByTestId("usage-tile-turns")).toHaveTextContent("10.0% failed")
    expect(screen.getByTestId("usage-tool-query")).toHaveTextContent("10.0%")
    expect(screen.getByTestId("usage-workspace-tokens-w1")).toHaveTextContent("Kisumu")
    expect(screen.getByTestId("usage-tenant-size-t1")).toHaveTextContent("4.0 KB")
    expect(screen.getByTestId("usage-schema-retained")).toHaveTextContent("1.0 KB")
    expect(mocked.dashboard).toHaveBeenCalledWith(30, expect.any(AbortSignal))
  })

  it("reloads for another time range", async () => {
    mocked.dashboard.mockResolvedValue(dashboard)
    render(<UsagePage />)
    await screen.findByTestId("usage-tile-dau")

    let finish: (value: UsageDashboard) => void = () => {}
    mocked.dashboard.mockReturnValueOnce(new Promise((resolve) => (finish = resolve)))
    await userEvent.click(screen.getByTestId("usage-days-7"))

    expect(screen.getByTestId("usage-refreshing")).toHaveTextContent("7 days")
    expect(screen.getByTestId("usage-dashboard")).toHaveAttribute("aria-busy", "true")
    finish({ ...dashboard, window: { ...dashboard.window, days: 7 } })
    await waitFor(() => expect(screen.queryByTestId("usage-refreshing")).not.toBeInTheDocument())
    expect(mocked.dashboard).toHaveBeenLastCalledWith(7, expect.any(AbortSignal))
    expect(screen.getByTestId("usage-days-7")).toHaveAttribute("aria-pressed", "true")
  })

  it("says so when the server refuses access", async () => {
    mocked.dashboard.mockRejectedValue(new ApiError(403, "Forbidden", {}))
    render(<UsagePage />)

    expect(await screen.findByTestId("usage-forbidden")).toBeInTheDocument()
  })

  it("offers a retry when loading fails", async () => {
    mocked.dashboard.mockRejectedValueOnce(new Error("network")).mockResolvedValue(dashboard)
    render(<UsagePage />)

    await userEvent.click(await screen.findByTestId("usage-retry"))

    expect(await screen.findByTestId("usage-tile-dau")).toBeInTheDocument()
  })
})

describe("Usage formatting", () => {
  it("formats durations and sizes", () => {
    expect(formatMs(null)).toBe("–")
    expect(formatMs(450)).toBe("450 ms")
    expect(formatMs(2500)).toBe("2.5 s")
    expect(formatMs(90000)).toBe("1.5 min")
    expect(formatBytes(512)).toBe("512 B")
    expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GB")
    expect(formatCompact(16_001_026)).toBe("16M")
    expect(formatCompact(839_281)).toBe("839.3K")
  })
})

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, renderHook } from "@testing-library/react"

// Mock the jobs API so the hook's polling is observable via call count.
const activeMock = vi.fn(async (workspaceId: string) => {
  void workspaceId
  return { jobs: [], recent_terminations: [] }
})
vi.mock("@/api/jobs", () => ({
  jobsApi: { active: (workspaceId: string) => activeMock(workspaceId) },
}))

import { useWorkspaceJobsImpl } from "@/hooks/useWorkspaceJobs"

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  })
  document.dispatchEvent(new Event("visibilitychange"))
}

describe("useWorkspaceJobs — visibility-gated polling (arch #254, 05#6)", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    activeMock.mockClear()
    setVisibility("visible")
  })
  afterEach(() => {
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
  })

  it("polls on an interval while the tab is visible", async () => {
    renderHook(() => useWorkspaceJobsImpl("ws-1"))
    expect(activeMock).toHaveBeenCalledTimes(1) // immediate fetch on mount
    await act(async () => {
      vi.advanceTimersByTime(3000)
    })
    expect(activeMock).toHaveBeenCalledTimes(2)
  })

  it("pauses polling while the tab is hidden", async () => {
    renderHook(() => useWorkspaceJobsImpl("ws-1"))
    expect(activeMock).toHaveBeenCalledTimes(1)

    await act(async () => {
      setVisibility("hidden")
    })
    const callsWhenHidden = activeMock.mock.calls.length

    await act(async () => {
      vi.advanceTimersByTime(9000) // three poll intervals
    })
    expect(activeMock).toHaveBeenCalledTimes(callsWhenHidden)
  })

  it("fires an immediate catch-up fetch when the tab becomes visible again", async () => {
    renderHook(() => useWorkspaceJobsImpl("ws-1"))
    await act(async () => {
      setVisibility("hidden")
    })
    const before = activeMock.mock.calls.length
    await act(async () => {
      setVisibility("visible")
    })
    expect(activeMock.mock.calls.length).toBeGreaterThan(before)
  })
})

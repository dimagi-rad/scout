import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { jobsApi, type ActiveJobsResponse, type PendingRequest, type WorkspaceLoad } from "@/api/jobs"
import { ApiError } from "@/api/client"
import { pollDelayMs, useWorkspaceJobsImpl } from "./useWorkspaceJobs"

const load: WorkspaceLoad = {
  tenant_id: "t1",
  tenant_name: "Clinic A",
  source_index: 2,
  source_total: 4,
  state: "loading",
  started_at: "2026-09-23T10:00:00Z",
  progress: null,
}

afterEach(() => vi.restoreAllMocks())

describe("useWorkspaceJobsImpl workspaceLoads", () => {
  it("does not show the previous workspace's load after a switch", async () => {
    vi.spyOn(jobsApi, "active").mockImplementation((id: string) =>
      id === "ws-a"
        ? Promise.resolve({ jobs: [], workspace_loads: [load], recent_terminations: [] })
        : new Promise(() => {}),
    )
    const { result, rerender } = renderHook(({ id }) => useWorkspaceJobsImpl(id), {
      initialProps: { id: "ws-a" },
    })
    await waitFor(() => expect(result.current.workspaceLoads).toHaveLength(1))

    rerender({ id: "ws-b" })

    expect(result.current.workspaceLoads).toEqual([])
  })
})

const held: PendingRequest = {
  thread_id: "thread-1",
  request_id: "r1",
  version: 1,
  parts: [{ id: "p1", text: "How many visits?", added_at: "2026-10-02T00:00:00Z" }],
  state: "waiting",
  thread_job_id: "job-1",
  thread_job_state: "pending",
}

function polls(...responses: ActiveJobsResponse[]) {
  const spy = vi.spyOn(jobsApi, "active")
  for (const response of responses) spy.mockResolvedValueOnce(response)
  spy.mockResolvedValue(responses.at(-1)!)
  return spy
}

const empty: ActiveJobsResponse = { jobs: [], recent_terminations: [], pending_requests: {} }
const holding: ActiveJobsResponse = { ...empty, pending_requests: { "thread-1": held } }

describe("useWorkspaceJobsImpl held requests", () => {
  it("reports a thread whose held request went out as just completed", async () => {
    polls(holding, empty)
    const { result } = renderHook(() => useWorkspaceJobsImpl("ws-a"))
    await waitFor(() => expect(result.current.pendingByThreadId["thread-1"]).toEqual(held))

    await act(() => result.current.refresh())

    expect(result.current.pendingByThreadId).toEqual({})
    expect(result.current.recentlyCompletedThreadIds).toEqual(["thread-1"])
  })

  it("shows a local change until a poll that started after it lands", async () => {
    let answer: (response: ActiveJobsResponse) => void = () => {}
    const spy = polls(empty)
    const { result } = renderHook(() => useWorkspaceJobsImpl("ws-a"))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    await act(async () => {})

    spy.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
    let inFlight: Promise<void> = Promise.resolve()
    act(() => {
      inFlight = result.current.refresh()
    })
    act(() => result.current.setPendingRequest("thread-1", held))
    // This poll left before the change, so its empty answer must not erase it.
    await act(async () => {
      answer(empty)
      await inFlight
    })
    expect(result.current.pendingByThreadId["thread-1"]).toEqual(held)

    spy.mockResolvedValueOnce(holding)
    await act(() => result.current.refresh())
    expect(result.current.pendingByThreadId["thread-1"]).toEqual(held)

    await act(() => result.current.refresh())
    expect(result.current.pendingByThreadId).toEqual({})
  })

  it("keeps a request this tab is sending hidden while the server still reports it", async () => {
    polls(holding)
    const { result } = renderHook(() => useWorkspaceJobsImpl("ws-a"))
    await waitFor(() => expect(result.current.pendingByThreadId["thread-1"]).toEqual(held))

    act(() => result.current.hidePendingRequest("thread-1", "r1"))
    await act(() => result.current.refresh())
    expect(result.current.pendingByThreadId).toEqual({})

    // A different request on the thread is not the one being sent.
    const next = { ...held, request_id: "r2" }
    vi.mocked(jobsApi.active).mockResolvedValue({ ...empty, pending_requests: { "thread-1": next } })
    await act(() => result.current.refresh())
    expect(result.current.pendingByThreadId["thread-1"]).toEqual(next)
  })

  it("keeps an in-flight change through polls until the caller settles it", async () => {
    polls(holding)
    const { result } = renderHook(() => useWorkspaceJobsImpl("ws-a"))
    await waitFor(() => expect(result.current.pendingByThreadId["thread-1"]).toEqual(held))

    act(() => result.current.setPendingRequest("thread-1", null, "inFlight"))
    await act(() => result.current.refresh())
    expect(result.current.pendingByThreadId).toEqual({})

    act(() => result.current.forgetPendingRequest("thread-1"))
    expect(result.current.pendingByThreadId["thread-1"]).toEqual(held)
  })

  it("ignores a poll that lands after a newer one", async () => {
    const spy = polls(empty)
    const { result } = renderHook(() => useWorkspaceJobsImpl("ws-a"))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    await act(async () => {})

    let answerOld: (response: ActiveJobsResponse) => void = () => {}
    spy.mockImplementationOnce(() => new Promise((resolve) => (answerOld = resolve)))
    spy.mockResolvedValueOnce(holding)
    let old: Promise<void> = Promise.resolve()
    act(() => {
      old = result.current.refresh()
    })
    await act(() => result.current.refresh())
    await act(async () => {
      answerOld(empty)
      await old
    })

    expect(result.current.pendingByThreadId["thread-1"]).toEqual(held)
  })
})

describe("pollDelayMs", () => {
  it("steps 3s, 10s, 30s, 60s and stays capped", () => {
    expect([0, 1, 2, 3, 4, 40].map(pollDelayMs)).toEqual([3000, 10000, 30000, 60000, 60000, 60000])
  })
})

describe("useWorkspaceJobsImpl poll backoff", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true })
  })

  const denied = () => new ApiError(403, "denied")

  async function advance(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms)
    })
  }

  it("backs off on 403, 503 and network errors up to 60s", async () => {
    const spy = vi
      .spyOn(jobsApi, "active")
      .mockRejectedValueOnce(denied())
      .mockRejectedValueOnce(new ApiError(503, "fresh"))
      .mockRejectedValueOnce(new TypeError("network"))
      .mockRejectedValue(new ApiError(500, "boom"))
    renderHook(() => useWorkspaceJobsImpl("ws-a"))
    await advance(0)
    expect(spy).toHaveBeenCalledTimes(1)
    await advance(9_999)
    expect(spy).toHaveBeenCalledTimes(1)
    await advance(1)
    expect(spy).toHaveBeenCalledTimes(2)
    await advance(30_000)
    expect(spy).toHaveBeenCalledTimes(3)
    await advance(60_000)
    expect(spy).toHaveBeenCalledTimes(4)
    await advance(59_999)
    expect(spy).toHaveBeenCalledTimes(4)
    await advance(1)
    expect(spy).toHaveBeenCalledTimes(5)
  })

  it("returns to 3s after a successful poll", async () => {
    const spy = vi
      .spyOn(jobsApi, "active")
      .mockRejectedValueOnce(denied())
      .mockResolvedValue(empty)
    renderHook(() => useWorkspaceJobsImpl("ws-a"))
    await advance(10_000)
    expect(spy).toHaveBeenCalledTimes(2)
    await advance(3_000)
    expect(spy).toHaveBeenCalledTimes(3)
  })

  it("resets to 3s and polls now when a job likely started", async () => {
    const spy = vi.spyOn(jobsApi, "active").mockRejectedValue(denied())
    const { result } = renderHook(() => useWorkspaceJobsImpl("ws-a"))
    await advance(10_000)
    expect(spy).toHaveBeenCalledTimes(2)
    act(() => result.current.notifyJobLikelyStarted())
    await advance(0)
    expect(spy).toHaveBeenCalledTimes(3)
    await advance(10_000)
    expect(spy).toHaveBeenCalledTimes(4)
  })

  it("resets the backoff on a workspace switch", async () => {
    const spy = vi.spyOn(jobsApi, "active").mockRejectedValue(denied())
    const { rerender } = renderHook(({ id }) => useWorkspaceJobsImpl(id), {
      initialProps: { id: "ws-a" },
    })
    await advance(10_000)
    await advance(30_000)
    expect(spy).toHaveBeenCalledTimes(3)
    rerender({ id: "ws-b" })
    await advance(0)
    expect(spy).toHaveBeenCalledTimes(4)
    await advance(10_000)
    expect(spy).toHaveBeenCalledTimes(5)
  })

  it("pauses while hidden and resets when the tab is visible again", async () => {
    const spy = vi.spyOn(jobsApi, "active").mockRejectedValue(denied())
    renderHook(() => useWorkspaceJobsImpl("ws-a"))
    await advance(10_000)
    await advance(30_000)
    expect(spy).toHaveBeenCalledTimes(3)
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true })
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"))
    })
    await advance(120_000)
    expect(spy).toHaveBeenCalledTimes(3)
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true })
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"))
    })
    await advance(0)
    expect(spy).toHaveBeenCalledTimes(4)
    await advance(10_000)
    expect(spy).toHaveBeenCalledTimes(5)
  })
})

import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { jobsApi, type ActiveJobsResponse, type PendingRequest, type WorkspaceLoad } from "@/api/jobs"
import { useWorkspaceJobsImpl } from "./useWorkspaceJobs"

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
})

import { act, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import type { PendingRequest } from "@/api/jobs"
import { useHeldRequest } from "./useHeldRequest"

const jobs = vi.hoisted(() => ({
  pendingByThreadId: {} as Record<string, unknown>,
  setPendingRequest: vi.fn(),
  hidePendingRequest: vi.fn(),
  forgetPendingRequest: vi.fn(),
  refresh: vi.fn(),
}))

vi.mock("@/contexts/WorkspaceJobsContext", () => ({ useWorkspaceJobs: () => jobs }))

const HELD: PendingRequest = {
  thread_id: "t1",
  request_id: "r1",
  version: 1,
  parts: [{ id: "m1", text: "Question", added_at: "2026-10-02T00:00:00Z" }],
  state: "waiting",
  thread_job_id: "job-1",
  thread_job_state: "pending",
}

describe("onMessagesLoaded", () => {
  afterEach(() => {
    delete jobs.pendingByThreadId.t1
  })

  it("clears the hidden messages of a load that ended before any turn", () => {
    const { result } = renderHook(() => useHeldRequest("w1", "t1"))
    act(() => result.current.onHeld(HELD))
    expect([...result.current.hiddenMessageIds]).toEqual(["m1"])

    act(() => result.current.onMessagesLoaded(HELD))
    expect(result.current.hiddenMessageIds.size).toBe(0)
  })

  it("keeps the hidden messages of a turn that is still running", () => {
    const { result } = renderHook(() => useHeldRequest("w1", "t1"))
    act(() => result.current.onHeld(HELD))

    act(() => result.current.onMessagesLoaded(HELD, { keepHidden: true }))
    expect([...result.current.hiddenMessageIds]).toEqual(["m1"])
  })

  it("shows the hidden messages again once the request is gone, even mid-turn", () => {
    const { result } = renderHook(() => useHeldRequest("w1", "t1"))
    act(() => result.current.onHeld(HELD))

    act(() => result.current.onMessagesLoaded(null, { keepHidden: true }))
    expect(result.current.hiddenMessageIds.size).toBe(0)
  })

  it("keeps them while the request is still displayed, even if the load predates it", () => {
    jobs.pendingByThreadId.t1 = HELD
    const { result } = renderHook(() => useHeldRequest("w1", "t1"))
    act(() => result.current.onHeld(HELD))

    act(() => result.current.onMessagesLoaded(null, { keepHidden: true }))
    expect([...result.current.hiddenMessageIds]).toEqual(["m1"])
  })
})

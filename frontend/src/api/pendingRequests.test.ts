import { describe, expect, it } from "vitest"

import type { JobState, PendingRequest } from "@/api/jobs"
import { pendingPhase, pendingRequestText } from "./pendingRequests"

function pending(state: PendingRequest["state"], jobState: JobState | null): PendingRequest {
  return {
    thread_id: "t",
    request_id: "r",
    version: 2,
    parts: [
      { id: "a", text: "visits?", added_at: "" },
      { id: "b", text: "by month", added_at: "" },
    ],
    state,
    thread_job_id: jobState ? "j" : null,
    thread_job_state: jobState,
  }
}

describe("pendingPhase", () => {
  it.each([
    ["waiting", "pending", "waiting"],
    ["claimed", "pending", "answering"],
    ["waiting", "running", "answering"],
    // One stopped while queued never resumes, so it must not spin forever.
    ["waiting", "cancelled", "unanswered"],
    ["waiting", "failed", "unanswered"],
    ["waiting", "completed", "unanswered"],
    ["waiting", null, "unanswered"],
  ] as const)("a %s request whose load is %s is %s", (state, jobState, phase) => {
    expect(pendingPhase(pending(state, jobState))).toBe(phase)
  })
})

it("joins the parts the way the server sends them", () => {
  expect(pendingRequestText(pending("waiting", "pending"))).toBe("visits?\n\nby month")
})

it("waits on a workspace load when the request has no load of its own", () => {
  const held = { ...pending("waiting", null), thread_job_id: null }
  expect(pendingPhase({ ...held, workspace_load_pending: true })).toBe("waiting")
  expect(pendingPhase({ ...held, workspace_load_pending: false })).toBe("unanswered")
})

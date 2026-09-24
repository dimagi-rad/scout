import { render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { ActiveJob, RecentTermination } from "@/api/jobs"
import type { TenantMembership } from "@/store/domainSlice"
import { useAppStore } from "@/store/store"
import { MaterializationFailure } from "./MaterializationFailure"
import { MaterializationProgressBanner } from "./MaterializationProgressBanner"

const WORKSPACE_ID = "ws-1"

const job: ActiveJob = {
  thread_job_id: "job-1",
  thread_id: "thread-1",
  tool_call_id: "call-1",
  job_type: "materialization",
  state: "running",
  progress: null,
  created_at: "2026-09-23T10:00:00Z",
}

const termination: RecentTermination = {
  thread_job_id: "job-1",
  thread_id: "thread-1",
  tool_call_id: "call-1",
  state: "failed",
  completed_at: "2026-09-23T10:05:00Z",
  error_summary: "Upstream timed out",
  retry_available: true,
}

function asRole(role: TenantMembership["role"]) {
  useAppStore.setState({ domains: [{ id: WORKSPACE_ID, role } as TenantMembership] })
}

afterEach(() => useAppStore.setState({ domains: [] }))

describe("materialization controls by role", () => {
  it.each([
    ["read", false],
    ["read_write", true],
    [null, true],
  ] as const)("%s members see Stop: %s", (role, visible) => {
    if (role) asRole(role)
    render(<MaterializationProgressBanner job={job} workspaceId={WORKSPACE_ID} />)

    expect(screen.getByTestId("materialization-progress-banner")).toBeInTheDocument()
    expect(screen.queryByTestId("materialization-banner-stop-btn") !== null).toBe(visible)
  })

  it.each([
    ["read", false],
    ["manage", true],
  ] as const)("%s members see Retry: %s", (role, visible) => {
    asRole(role)
    render(
      <MaterializationFailure
        termination={termination}
        workspaceId={WORKSPACE_ID}
        threadId="thread-1"
      />,
    )

    expect(screen.getByTestId("materialization-failure-summary")).toHaveTextContent(
      "Upstream timed out",
    )
    expect(screen.queryByTestId("materialization-retry-btn") !== null).toBe(visible)
  })
})

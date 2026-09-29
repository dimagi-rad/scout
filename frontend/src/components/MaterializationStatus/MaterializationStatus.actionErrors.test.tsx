import { fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api, ApiError } from "@/api/client"
import { jobsApi, type ActiveJob, type RecentTermination } from "@/api/jobs"
import type { TenantMembership } from "@/store/domainSlice"
import { useAppStore } from "@/store/store"
import { MaterializationFailure } from "./MaterializationFailure"
import { MaterializationProgressBanner } from "./MaterializationProgressBanner"

const WORKSPACE_ID = "ws-1"

const termination: RecentTermination = {
  thread_job_id: "job-1",
  thread_id: "thread-1",
  tool_call_id: "call-1",
  state: "failed",
  completed_at: "2026-09-23T10:05:00Z",
  error_summary: "Upstream timed out",
  retry_available: true,
}

const job: ActiveJob = {
  thread_job_id: "job-1",
  thread_id: "thread-1",
  tool_call_id: "call-1",
  job_type: "materialization",
  state: "running",
  progress: null,
  created_at: "2026-09-23T10:00:00Z",
}

const VERIFICATION_MESSAGE =
  "We couldn't verify your access to this workspace right now. Please retry shortly."
const COVERAGE_MESSAGE =
  "This workspace requires access to every one of its data sources. Still needed — 'Clinic': " +
  "connect CommCare team 'north' in Connected Accounts."

// Shapes mirror apps/workspaces/access.py:access_denied_body.
const verificationDenial = new ApiError(403, VERIFICATION_MESSAGE, {
  error: VERIFICATION_MESSAGE,
  reason: "verification_unavailable",
  retryable: true,
  recovery_url: "/settings/connections",
})
const coverageDenial = new ApiError(403, COVERAGE_MESSAGE, {
  error: COVERAGE_MESSAGE,
  reason: "tenant_access_lost",
  lost_tenants: ["Clinic"],
  missing_tenants: [],
})
// A proxy's HTML error page: responseError finds no JSON and uses statusText.
const htmlServerError = new ApiError(500, "Internal Server Error", undefined)

function renderFailure() {
  useAppStore.setState({ domains: [{ id: WORKSPACE_ID, role: "read_write" } as TenantMembership] })
  render(
    <MaterializationFailure termination={termination} workspaceId={WORKSPACE_ID} threadId="thread-1" />,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  useAppStore.setState({ domains: [] })
})

describe("MaterializationFailure retry errors", () => {
  it("shows a retryable denial's message and keeps Retry offered", async () => {
    vi.spyOn(jobsApi, "retryMaterialization").mockRejectedValue(verificationDenial)
    renderFailure()

    fireEvent.click(screen.getByTestId("materialization-retry-btn"))

    expect(await screen.findByTestId("materialization-retry-error")).toHaveTextContent(
      VERIFICATION_MESSAGE,
    )
    expect(screen.getByTestId("materialization-retry-btn")).toBeInTheDocument()
  })

  it("shows a non-retryable denial's message and withdraws Retry", async () => {
    vi.spyOn(jobsApi, "retryMaterialization").mockRejectedValue(coverageDenial)
    renderFailure()

    fireEvent.click(screen.getByTestId("materialization-retry-btn"))

    expect(await screen.findByTestId("materialization-retry-error")).toHaveTextContent(
      COVERAGE_MESSAGE,
    )
    expect(screen.queryByTestId("materialization-retry-btn")).not.toBeInTheDocument()
  })

  it("keeps Retry offered after a non-JSON 403 such as a CSRF failure", async () => {
    vi.spyOn(jobsApi, "retryMaterialization").mockRejectedValue(
      new ApiError(403, "Forbidden", undefined),
    )
    renderFailure()

    fireEvent.click(screen.getByTestId("materialization-retry-btn"))

    expect(await screen.findByTestId("materialization-retry-error")).toHaveTextContent(
      "Retry failed — try again",
    )
    expect(screen.getByTestId("materialization-retry-btn")).toBeInTheDocument()
  })

  it("falls back to the generic text for a non-JSON 500", async () => {
    vi.spyOn(jobsApi, "retryMaterialization").mockRejectedValue(htmlServerError)
    renderFailure()

    fireEvent.click(screen.getByTestId("materialization-retry-btn"))

    expect(await screen.findByTestId("materialization-retry-error")).toHaveTextContent(
      "Retry failed — try again",
    )
    expect(screen.getByTestId("materialization-retry-btn")).toBeInTheDocument()
  })
})

describe("MaterializationProgressBanner cancel errors", () => {
  it("shows a retryable denial's message and keeps Stop offered", async () => {
    vi.spyOn(api, "post").mockRejectedValue(verificationDenial)
    render(<MaterializationProgressBanner job={job} workspaceId={WORKSPACE_ID} />)

    fireEvent.click(screen.getByTestId("materialization-banner-stop-btn"))

    expect(await screen.findByTestId("materialization-banner-cancel-error")).toHaveTextContent(
      VERIFICATION_MESSAGE,
    )
    expect(screen.getByTestId("materialization-banner-stop-btn")).toBeInTheDocument()
  })

  it("shows a non-retryable denial's message and withdraws Stop", async () => {
    vi.spyOn(api, "post").mockRejectedValue(coverageDenial)
    render(<MaterializationProgressBanner job={job} workspaceId={WORKSPACE_ID} />)

    fireEvent.click(screen.getByTestId("materialization-banner-stop-btn"))

    expect(await screen.findByTestId("materialization-banner-cancel-error")).toHaveTextContent(
      COVERAGE_MESSAGE,
    )
    expect(screen.queryByTestId("materialization-banner-stop-btn")).not.toBeInTheDocument()
  })
})

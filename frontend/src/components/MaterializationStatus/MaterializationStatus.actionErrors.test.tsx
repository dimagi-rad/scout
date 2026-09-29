import { act, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api, ApiError } from "@/api/client"
import { jobsApi } from "@/api/jobs"
import type { TenantMembership } from "@/store/domainSlice"
import { useAppStore } from "@/store/store"
import { MaterializationFailure } from "./MaterializationFailure"
import { MaterializationProgressBanner } from "./MaterializationProgressBanner"
import { job, termination, WORKSPACE_ID } from "./testFixtures"

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
  vi.useRealTimers()
  useAppStore.setState({ domains: [] })
})

describe("MaterializationFailure retry errors", () => {
  it("shows a retryable denial's message and re-arms Retry", async () => {
    vi.spyOn(jobsApi, "retryMaterialization").mockRejectedValue(verificationDenial)
    renderFailure()

    fireEvent.click(screen.getByTestId("materialization-retry-btn"))

    expect(await screen.findByTestId("materialization-retry-error")).toHaveTextContent(
      VERIFICATION_MESSAGE,
    )
    expect(screen.getByTestId("materialization-retry-btn")).toBeEnabled()
  })

  it("clears a retryable failure once it has been shown", async () => {
    vi.useFakeTimers()
    vi.spyOn(jobsApi, "retryMaterialization").mockRejectedValue(verificationDenial)
    renderFailure()

    await act(async () => fireEvent.click(screen.getByTestId("materialization-retry-btn")))
    expect(screen.getByTestId("materialization-retry-error")).toBeInTheDocument()

    act(() => vi.advanceTimersByTime(10_000))
    expect(screen.queryByTestId("materialization-retry-error")).not.toBeInTheDocument()
  })

  it("shows a final denial's message and disables Retry until the user comes back", async () => {
    vi.spyOn(jobsApi, "retryMaterialization").mockRejectedValue(coverageDenial)
    renderFailure()

    fireEvent.click(screen.getByTestId("materialization-retry-btn"))

    expect(await screen.findByTestId("materialization-retry-error")).toHaveTextContent(
      COVERAGE_MESSAGE,
    )
    expect(screen.getByTestId("materialization-retry-btn")).toBeDisabled()

    // A focus without leaving first must not erase the unread denial.
    act(() => {
      window.dispatchEvent(new Event("focus"))
    })
    expect(screen.getByTestId("materialization-retry-btn")).toBeDisabled()

    // e.g. after connecting the missing source in another tab
    act(() => {
      window.dispatchEvent(new Event("blur"))
      window.dispatchEvent(new Event("focus"))
    })
    expect(screen.getByTestId("materialization-retry-btn")).toBeEnabled()
    expect(screen.queryByTestId("materialization-retry-error")).not.toBeInTheDocument()
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
    expect(screen.getByTestId("materialization-retry-btn")).toBeEnabled()
  })

  it("falls back to the generic text for a non-JSON 500", async () => {
    vi.spyOn(jobsApi, "retryMaterialization").mockRejectedValue(htmlServerError)
    renderFailure()

    fireEvent.click(screen.getByTestId("materialization-retry-btn"))

    expect(await screen.findByTestId("materialization-retry-error")).toHaveTextContent(
      "Retry failed — try again",
    )
    expect(screen.getByTestId("materialization-retry-btn")).toBeEnabled()
  })
})

describe("MaterializationProgressBanner cancel errors", () => {
  it("shows a final denial's message and disables Stop", async () => {
    vi.spyOn(api, "post").mockRejectedValue(coverageDenial)
    render(<MaterializationProgressBanner job={job} workspaceId={WORKSPACE_ID} />)

    fireEvent.click(screen.getByTestId("materialization-banner-stop-btn"))

    expect(await screen.findByTestId("materialization-banner-cancel-error")).toHaveTextContent(
      COVERAGE_MESSAGE,
    )
    expect(screen.getByTestId("materialization-banner-stop-btn")).toBeDisabled()
  })

  it("falls back to the generic text for a non-JSON 500 and keeps Stop", async () => {
    vi.spyOn(api, "post").mockRejectedValue(htmlServerError)
    render(<MaterializationProgressBanner job={job} workspaceId={WORKSPACE_ID} />)

    fireEvent.click(screen.getByTestId("materialization-banner-stop-btn"))

    expect(await screen.findByTestId("materialization-banner-cancel-error")).toHaveTextContent(
      "Cancel failed — try again",
    )
    expect(screen.getByTestId("materialization-banner-stop-btn")).toBeEnabled()
  })
})

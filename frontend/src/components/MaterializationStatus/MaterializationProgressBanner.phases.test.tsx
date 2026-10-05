import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import type { ActiveJob, JobProgress } from "@/api/jobs"
import { MaterializationProgressBanner } from "./MaterializationProgressBanner"
import { job, WORKSPACE_ID } from "./testFixtures"

const baseProgress: JobProgress = {
  percent: null,
  rows_loaded: 0,
  rows_total: null,
  message: null,
  source: null,
  step: 10,
  total_steps: 10,
}

function renderJob(overrides: Partial<ActiveJob>) {
  render(
    <MaterializationProgressBanner
      job={{ ...job, state: "pending", ...overrides }}
      workspaceId={WORKSPACE_ID}
    />,
  )
}

describe("post-load phases", () => {
  it.each([
    ["building_tables", "Building tables", "Stage 1 of 2 · 12 models"],
    ["checking_quality", "Checking data quality", "Running data tests on the new tables"],
    ["combining_sites", "Combining sites", "Joining 3 sources into one view"],
    ["building_model", "Building the data model", "Preparing the tables and measures"],
    ["finishing", "Finishing up", "Updating views that share these sources"],
  ] as const)("%s shows its title and message, not the step count", (phase, title, message) => {
    renderJob({ progress: { ...baseProgress, phase, message } })

    expect(screen.getByTestId("materialization-banner-source")).toHaveTextContent(title)
    expect(screen.getByTestId("materialization-banner-rows")).toHaveTextContent(message)
    expect(screen.queryByTestId("materialization-banner-step")).toBeNull()
    expect(screen.queryByTestId("materialization-banner-answering-icon")).toBeNull()
  })

  it("keeps the step count and source while sources load", () => {
    renderJob({
      progress: { ...baseProgress, step: 3, source: "cases", rows_loaded: 50, rows_total: 100, percent: 50 },
    })

    expect(screen.getByTestId("materialization-banner-source")).toHaveTextContent("Fetching cases")
    expect(screen.getByTestId("materialization-banner-step")).toHaveTextContent("Step 3 of 10")
    expect(screen.getByTestId("materialization-banner-percent")).toHaveTextContent("50%")
  })

  it("shows the agent answering once the job is running, even without a phase", () => {
    renderJob({ state: "running", progress: { ...baseProgress, phase: "finishing" } })

    expect(screen.getByTestId("materialization-banner-source")).toHaveTextContent(
      "Writing your answer",
    )
    expect(screen.getByTestId("materialization-banner-answering-icon")).toBeInTheDocument()
    expect(screen.queryByTestId("materialization-banner-step")).toBeNull()
  })

  it("falls back to the generic text for a server that sends no phase", () => {
    renderJob({ progress: baseProgress })

    expect(screen.getByTestId("materialization-banner-source")).toHaveTextContent(
      "Materializing data",
    )
    expect(screen.getByTestId("materialization-banner-rows")).toHaveTextContent("Preparing…")
  })
})

describe("load time estimate", () => {
  const time_estimate = {
    usual_seconds: 240, elapsed_seconds: 120, sample_count: 5, phase_seconds: {},
  }
  it("shows approximate timing on an active job", () => {
    renderJob({ time_estimate })
    expect(screen.getByTestId("materialization-banner-time-estimate"))
      .toHaveTextContent("About 2 min left")
  })
  it("hides load timing while writing the answer", () => {
    renderJob({ state: "running", time_estimate })
    expect(screen.queryByTestId("materialization-banner-time-estimate")).toBeNull()
  })
  it("shows timing for another member's workspace load", () => {
    render(<MaterializationProgressBanner workspaceId={WORKSPACE_ID} load={{
      tenant_id: "tenant", tenant_name: "Site", source_index: 1, source_total: 1,
      state: "loading", started_at: "2026-01-01T00:00:00Z", progress: baseProgress,
      time_estimate,
    }} />)
    expect(screen.getByTestId("materialization-banner-time-estimate"))
      .toHaveTextContent("About 2 min left")
  })
})

import { render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { useAppStore } from "@/store/store"
import { MaterializationFailure } from "./MaterializationFailure"
import { MaterializationProgressBanner } from "./MaterializationProgressBanner"
import { job, termination, WORKSPACE_ID } from "./testFixtures"
import type { WorkspaceListItem } from "@/api/workspaces"

function asRole(role: WorkspaceListItem["role"]) {
  useAppStore.setState({ domains: [{ id: WORKSPACE_ID, role } as WorkspaceListItem] })
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

describe("source position and teammates' loads", () => {
  it("labels which source of how many is loading", () => {
    render(
      <MaterializationProgressBanner
        job={{
          ...job,
          progress: {
            percent: null,
            rows_loaded: 0,
            rows_total: null,
            message: null,
            source: null,
            step: 2,
            total_steps: 5,
          },
          source_index: 2,
          source_total: 4,
          tenant_name: "Clinic B",
        }}
        workspaceId={WORKSPACE_ID}
      />,
    )

    expect(screen.getByTestId("materialization-banner-source-position")).toHaveTextContent(
      "Source 2 of 4 · Clinic B",
    )
    expect(screen.getByTestId("materialization-banner-step")).toHaveTextContent("Step 2 of 5")
  })

  it("shows another member's load without Stop", () => {
    asRole("manage")
    render(
      <MaterializationProgressBanner
        load={{
          tenant_id: "t1",
          tenant_name: "Clinic A",
          source_index: 1,
          source_total: 3,
          state: "loading",
          started_at: "2026-09-23T10:00:00Z",
          progress: null,
        }}
        workspaceId={WORKSPACE_ID}
      />,
    )

    expect(screen.getByTestId("workspace-load-banner")).toBeInTheDocument()
    expect(screen.getByTestId("materialization-banner-source-position")).toHaveTextContent(
      "Source 1 of 3 · Clinic A",
    )
    expect(screen.queryByTestId("materialization-banner-stop-btn")).toBeNull()
  })
})

import { renderHook, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { jobsApi, type WorkspaceLoad } from "@/api/jobs"
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

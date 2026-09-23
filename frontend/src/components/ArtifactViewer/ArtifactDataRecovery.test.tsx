import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import type { TenantMembership } from "@/store/domainSlice"
import { useAppStore } from "@/store/store"
import type { ArtifactDetail } from "@/components/ArtifactGraph"
import { ArtifactCanvas } from "./ArtifactCanvas"

vi.mock("@/components/ArtifactGraph", () => ({
  ArtifactGraphRenderer: () => <div data-testid="artifact-graph-renderer">Rendered graph</div>,
}))

const ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"
const WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"

const artifact: ArtifactDetail = {
  id: ARTIFACT_ID,
  title: "Visit trends",
  type: "story",
  code: "",
  data: { story_doc: { schema_version: 1, blocks: [] } },
  semantic_queries: [{ name: "visits", measures: ["visits.count"] }],
  version: 1,
}

const endpoint = `/api/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/recovery/`

describe("ArtifactDataRecovery", () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it("renders the artifact only after its data surface is ready", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue({
      status: "ready",
      recovery_action: null,
      physical_status: "active",
      semantic_status: "ready",
      message: "Artifact data is ready.",
      can_retry: false,
      recovery: null,
    })

    render(
      <ArtifactCanvas
        artifactId={ARTIFACT_ID}
        workspaceId={WORKSPACE_ID}
        artifact={artifact}
        isLoading={false}
        error={null}
      />,
    )

    expect(screen.getByTestId("artifact-data-checking")).toBeInTheDocument()
    expect(await screen.findByTestId("artifact-graph-renderer")).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith(endpoint)
  })

  it("offers an explicit restore action and shows durable progress", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      status: "needs_materialization",
      recovery_action: "materialization",
      physical_status: "expired",
      semantic_status: "blocked",
      message: "The data behind this artifact is no longer available.",
      can_retry: true,
      recovery: null,
    })
    const post = vi.spyOn(api, "post").mockResolvedValue({
      status: "recovering",
      recovery_action: "materialization",
      physical_status: "expired",
      semantic_status: "blocked",
      message: "Restoring the workspace data for this artifact.",
      can_retry: false,
      recovery: {
        id: "33333333-3333-3333-3333-333333333333",
        type: "materialization",
        state: "running",
        progress: {
          percent: 40,
          rows_loaded: 400,
          rows_total: 1_000,
          unit: "rows",
          message: "Loading visits",
          source: "visits",
          step: 1,
          total_steps: 2,
        },
        created_at: "2026-09-14T12:00:00Z",
      },
    })

    render(
      <ArtifactCanvas
        artifactId={ARTIFACT_ID}
        workspaceId={WORKSPACE_ID}
        artifact={artifact}
        isLoading={false}
        error={null}
      />,
    )

    await userEvent.click(await screen.findByTestId("artifact-data-recover"))

    expect(post).toHaveBeenCalledWith(endpoint, {})
    expect(await screen.findByText("Restoring artifact data")).toBeInTheDocument()
    expect(screen.getByText("40%")).toBeInTheDocument()
    expect(screen.getByText("400 rows loaded")).toBeInTheDocument()
    expect(screen.queryByTestId("artifact-graph-renderer")).not.toBeInTheDocument()
  })

  it("disables restore with an explanation for read-only members", async () => {
    useAppStore.setState({
      domains: [{ id: WORKSPACE_ID, role: "read" } as TenantMembership],
    })
    vi.spyOn(api, "get").mockResolvedValue({
      status: "needs_materialization",
      recovery_action: "materialization",
      physical_status: "expired",
      semantic_status: "blocked",
      message: "The data behind this artifact is no longer available.",
      can_retry: true,
      recovery: null,
    })
    const post = vi.spyOn(api, "post")

    try {
      render(
        <ArtifactCanvas
          artifactId={ARTIFACT_ID}
          workspaceId={WORKSPACE_ID}
          artifact={artifact}
          isLoading={false}
          error={null}
        />,
      )

      const restore = await screen.findByTestId("artifact-data-recover")
      expect(restore).toBeDisabled()
      expect(screen.getByTestId("artifact-data-recover-readonly-hint")).toHaveTextContent(
        "Read-only access",
      )
      await userEvent.click(restore)
      expect(post).not.toHaveBeenCalled()
    } finally {
      useAppStore.setState({ domains: [] })
    }
  })

  it("keeps failed recovery retryable", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      status: "failed",
      recovery_action: "semantic_rebuild",
      physical_status: "active",
      semantic_status: "unavailable",
      message: "Cube rejected schema",
      can_retry: true,
      recovery: {
        id: "33333333-3333-3333-3333-333333333333",
        type: "semantic_rebuild",
        state: "failed",
        progress: null,
        created_at: "2026-09-14T12:00:00Z",
      },
    })

    render(
      <ArtifactCanvas
        artifactId={ARTIFACT_ID}
        workspaceId={WORKSPACE_ID}
        artifact={artifact}
        isLoading={false}
        error={null}
      />,
    )

    expect(await screen.findByText("Data recovery failed")).toBeInTheDocument()
    expect(screen.getByText("Cube rejected schema")).toBeInTheDocument()
    expect(screen.getByTestId("artifact-data-recover")).toHaveTextContent("Try recovery again")
    await waitFor(() => expect(screen.queryByTestId("artifact-data-checking")).not.toBeInTheDocument())
  })

  it("keeps readable charts mounted through a failed repair, retry, polling error, and later success", async () => {
    vi.useFakeTimers()
    const stale = {
      status: "failed",
      queryable: true,
      data_revision: "last-published",
      recovery_action: "semantic_rebuild",
      physical_status: "active",
      semantic_status: "stale",
      message: "Showing the last available data. The latest repair did not complete.",
      detail: "Synthetic schema rejection",
      can_retry: true,
      recovery: null,
    }
    vi.spyOn(api, "get")
      .mockResolvedValueOnce(stale)
      .mockRejectedValueOnce(new Error("Temporary status check failure"))
      .mockResolvedValueOnce({ ...stale, status: "ready", data_revision: "newly-published", semantic_status: "ready", recovery_action: null, detail: undefined })
    const post = vi.spyOn(api, "post").mockResolvedValue({
      ...stale,
      status: "recovering",
      can_retry: false,
      recovery: { type: "semantic_rebuild", state: "running", progress: null },
    })
    render(<ArtifactCanvas artifactId={ARTIFACT_ID} workspaceId={WORKSPACE_ID} artifact={artifact} isLoading={false} error={null} />)
    await act(async () => {})
    const graph = screen.getByTestId("artifact-graph-renderer")
    expect(screen.getByTestId("artifact-data-warning")).toHaveTextContent("Synthetic schema rejection")
    expect(screen.queryByTestId("artifact-data-recovery")).not.toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByTestId("artifact-data-recover")) })
    expect(post).toHaveBeenCalledWith(endpoint, {})
    expect(screen.getByTestId("artifact-data-warning")).toHaveTextContent("Your artifact remains available while recovery runs.")
    expect(screen.getByTestId("artifact-graph-renderer")).toBe(graph)
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(screen.getByTestId("artifact-data-warning")).toHaveTextContent("Temporary status check failure")
    expect(screen.getByTestId("artifact-graph-renderer")).toBe(graph)
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(screen.getByTestId("artifact-graph-renderer")).toBe(graph)
    expect(screen.queryByTestId("artifact-data-warning")).not.toBeInTheDocument()
  })

  it("discloses stale-but-readable data before any artifact recovery was attempted", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      status: "ready", queryable: true, recovery_action: "semantic_rebuild",
      semantic_status: "stale", physical_status: "active", can_retry: true, recovery: null,
      message: "Showing the last available data.", detail: "The latest data model rebuild did not complete.",
    })
    render(<ArtifactCanvas artifactId={ARTIFACT_ID} workspaceId={WORKSPACE_ID} artifact={artifact} isLoading={false} error={null} />)
    expect(await screen.findByTestId("artifact-graph-renderer")).toBeInTheDocument()
    expect(screen.getByTestId("artifact-data-warning")).toHaveTextContent("The latest data model rebuild did not complete.")
    expect(screen.getByTestId("artifact-data-recover")).toHaveTextContent("Rebuild data model")
  })

  it("shows model-repair guidance without offering a provider reload", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      status: "model_drift", queryable: false, recovery_action: null,
      semantic_status: "ready", physical_status: "active", can_retry: false, recovery: null,
      message: "This artifact references a field that is no longer available.",
      detail: "Review its data model or update the artifact in chat.",
    })
    render(<ArtifactCanvas artifactId={ARTIFACT_ID} workspaceId={WORKSPACE_ID} artifact={artifact} isLoading={false} error={null} />)
    expect(await screen.findByText("Artifact data model changed")).toBeInTheDocument()
    expect(screen.getByText("Review its data model or update the artifact in chat.")).toBeInTheDocument()
    expect(screen.queryByTestId("artifact-data-recover")).not.toBeInTheDocument()
    expect(screen.queryByTestId("artifact-graph-renderer")).not.toBeInTheDocument()
  })

  it("keeps the iframe sandbox unchanged when readable data has a recovery warning", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      status: "ready", queryable: true, recovery_action: "semantic_rebuild",
      semantic_status: "stale", physical_status: "active", can_retry: true, recovery: null,
      message: "Showing the last available data.",
    })
    render(<ArtifactCanvas artifactId={ARTIFACT_ID} workspaceId={WORKSPACE_ID} artifact={{ ...artifact, type: "react" }} isLoading={false} error={null} />)
    const frame = await screen.findByTestId(`artifact-frame-${ARTIFACT_ID}`)
    expect(frame).toHaveAttribute("sandbox", "allow-scripts allow-modals")
    expect(screen.getByTestId("artifact-data-warning")).toBeInTheDocument()
  })
})

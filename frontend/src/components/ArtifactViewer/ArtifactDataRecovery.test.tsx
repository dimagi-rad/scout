import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
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
})

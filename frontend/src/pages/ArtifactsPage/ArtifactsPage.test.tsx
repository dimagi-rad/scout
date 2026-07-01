import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"

import { api } from "@/api/client"
import { useAppStore } from "@/store/store"
import { ArtifactDetailPage } from "@/pages/ArtifactDetailPage"
import { ArtifactsPage } from "./ArtifactsPage"
import type { ArtifactSummary } from "@/store/artifactSlice"

const WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
const ARTIFACT_ID = "artifact-a1"

function artifact(id: string, title: string): ArtifactSummary {
  return {
    id,
    title,
    description: `Artifact ${title}`,
    artifact_type: "react",
    version: 1,
    has_live_queries: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  }
}

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname}</div>
}

describe("ArtifactsPage navigation", () => {
  beforeEach(() => {
    useAppStore.setState({
      activeDomainId: WORKSPACE_ID,
      artifacts: [],
      artifactsStatus: "idle",
      artifactSearch: "",
      activeArtifactId: null,
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("opens artifact cards on a canonical detail URL and returns to the list", async () => {
    const getSpy = vi.spyOn(api, "get").mockImplementation(async (url: string) => {
      if (url === `/api/workspaces/${WORKSPACE_ID}/artifacts/`) {
        return { results: [artifact(ARTIFACT_ID, "Alpha Dashboard")] } as never
      }
      if (url === `/api/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/data/`) {
        return {
          id: ARTIFACT_ID,
          title: "Alpha Dashboard",
          type: "react",
          code: "export default function Dashboard() { return <div /> }",
          data: {},
          semantic_queries: [],
          version: 1,
        } as never
      }
      if (url === `/api/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/query-data/`) {
        return {
          queries: [
            {
              name: "approved_by_worker",
              semantic_query: { measures: ["approved_count"] },
              columns: ["worker", "approved_count"],
              rows: [["alice", 10]],
              row_count: 1,
            },
            {
              name: "completed_by_worker",
              semantic_query: { measures: ["completed_count"] },
              columns: ["worker", "completed_count"],
              rows: [["alice", 12]],
              row_count: 1,
            },
          ],
          static_data: {},
          semantic_query_manifest: {
            generated_at: "2026-07-01T14:46:55.310346+00:00",
            entries: [{ query_key: "approved_by_worker" }, { query_key: "completed_by_worker" }],
          },
        } as never
      }
      throw new Error(`Unexpected request: ${url}`)
    })

    render(
      <MemoryRouter initialEntries={["/artifacts"]}>
        <Routes>
          <Route path="/artifacts" element={<><LocationProbe /><ArtifactsPage /></>} />
          <Route path="/artifacts/:artifactId" element={<><LocationProbe /><ArtifactDetailPage /></>} />
        </Routes>
      </MemoryRouter>,
    )

    await screen.findByText("Alpha Dashboard")

    await userEvent.click(screen.getByTestId(`artifact-open-${ARTIFACT_ID}`))

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent(`/artifacts/${ARTIFACT_ID}`)
    })
    expect(screen.getByTestId("artifact-detail-page")).toBeInTheDocument()
    expect(await screen.findByTestId("artifact-detail-title")).toHaveTextContent("Alpha Dashboard")
    expect(screen.queryByTestId("artifact-tab-view")).not.toBeInTheDocument()
    expect(screen.getByTestId("artifact-view-data")).toHaveTextContent("View Data")

    const frame = await screen.findByTestId(`artifact-frame-${ARTIFACT_ID}`)
    expect(frame).toHaveAttribute(
      "src",
      `/api/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/sandbox/`,
    )
    expect(getSpy).toHaveBeenCalledWith(`/api/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/data/`)

    await userEvent.click(screen.getByTestId("artifact-view-data"))

    expect(await screen.findByTestId("artifact-data-panel")).toBeInTheDocument()
    expect(screen.getByRole("dialog")).toBeInTheDocument()
    expect(getSpy).toHaveBeenCalledWith(`/api/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/query-data/`)
    expect(await screen.findByText("approved_by_worker")).toBeInTheDocument()
    expect(screen.getByText("completed_by_worker")).toBeInTheDocument()
    expect(screen.queryByText("2 queries")).not.toBeInTheDocument()
    expect(screen.queryByText("Semantic dependencies")).not.toBeInTheDocument()
    expect(screen.getByTestId("artifact-export-pdf")).not.toBeDisabled()
    expect(screen.getByTestId(`artifact-frame-${ARTIFACT_ID}`)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Artifact" })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole("button", { name: "Close" }))
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
    })

    await userEvent.click(screen.getByTestId("artifact-back-link"))

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/artifacts")
    })
    expect(await screen.findByText("Alpha Dashboard")).toBeInTheDocument()
  })
})

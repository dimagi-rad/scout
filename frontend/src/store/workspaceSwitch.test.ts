import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, render, screen, waitFor } from "@testing-library/react"
import { createElement, useEffect } from "react"
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { api } from "@/api/client"
import { workspaceApi } from "@/api/workspaces"
import { RecipesPage } from "@/pages/RecipesPage/RecipesPage"
import { ArtifactsPage } from "@/pages/ArtifactsPage/ArtifactsPage"
import { WorkspaceDetailPage } from "@/pages/WorkspaceDetailPage/WorkspaceDetailPage"
import { ApiError } from "@/api/client"
import type { Recipe } from "@/store/recipeSlice"
import type { ArtifactSummary } from "@/store/artifactSlice"
import type { WorkspaceDetail } from "@/api/workspaces"

// The active workspace id is baked into the `/api/workspaces/{id}/...` request
// path, so the mocked api call's url proves a refetch fired for the NEW workspace.
const WS_A = "11111111-1111-1111-1111-111111111111"
const WS_B = "22222222-2222-2222-2222-222222222222"

/** Pull the workspace UUID out of a `/api/workspaces/{id}/...` request URL. */
function workspaceIdFromUrl(url: string): string | null {
  const match = url.match(/\/api\/workspaces\/([^/?]+)\//)
  return match ? match[1] : null
}

function recipe(id: string, name: string): Recipe {
  return {
    id,
    name,
    description: `Recipe ${name}`,
    prompt: "do the thing",
    variables: [],
    is_shared: false,
    variable_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  }
}

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

function workspaceDetail(id: string, name: string): WorkspaceDetail {
  return {
    id,
    name,
    display_name: name,
    is_auto_created: false,
    role: "manage",
    system_prompt: "",
    schema_status: "available",
    tenant_count: 1,
    member_count: 1,
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  }
}

/**
 * Workspace-switch state-reset contract (issue #247).
 *
 * Switching workspaces starts a fresh per-workspace thread (00c423d threadId-leak
 * fix). #247 additionally keys page effects on `activeDomainId` and clears
 * WorkspaceDetailPage's `error` per load; the refetch/clear tests assert that by
 * rendering the page against a mocked api layer and a real store.
 */
describe("workspace switch resets per-workspace state", () => {
  beforeEach(() => {
    useAppStore.setState({ activeDomainId: "ws-a", threadId: "thread-a" })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("starts a fresh thread for the new workspace (no cross-workspace carry)", () => {
    const before = useAppStore.getState().threadId
    useAppStore.getState().domainActions.setActiveDomain("ws-b")
    expect(useAppStore.getState().threadId).not.toBe(before)
  })

  it("refetches the recipes list on workspace switch (#247, 04#9)", async () => {
    const recipesByWs: Record<string, Recipe[]> = {
      [WS_A]: [recipe("recipe-a1", "Alpha Cohort Report")],
      [WS_B]: [recipe("recipe-b1", "Beta Revenue Rollup")],
    }
    const getSpy = vi
      .spyOn(api, "get")
      .mockImplementation(async (url: string) => {
        const wsId = workspaceIdFromUrl(url)
        return (wsId ? recipesByWs[wsId] : []) as never
      })

    useAppStore.setState({
      activeDomainId: WS_A,
      recipes: [],
      recipeStatus: "idle",
    })

    render(
      createElement(
        MemoryRouter,
        { initialEntries: ["/recipes"] },
        createElement(
          Routes,
          null,
          createElement(Route, { path: "/recipes", element: createElement(RecipesPage) }),
        ),
      ),
    )

    await waitFor(() => expect(screen.getByTestId("recipe-card-recipe-a1")).toBeInTheDocument())
    expect(screen.queryByTestId("recipe-card-recipe-b1")).not.toBeInTheDocument()
    expect(getSpy).toHaveBeenCalledWith(`/api/workspaces/${WS_A}/recipes/`)

    act(() => {
      useAppStore.getState().domainActions.setActiveDomain(WS_B)
    })

    // Refetches for the NEW workspace id. (Old code only fetched on mount → fails.)
    await waitFor(() => expect(screen.getByTestId("recipe-card-recipe-b1")).toBeInTheDocument())
    expect(getSpy).toHaveBeenCalledWith(`/api/workspaces/${WS_B}/recipes/`)
    expect(screen.queryByTestId("recipe-card-recipe-a1")).not.toBeInTheDocument()
  })

  it("refetches the artifacts list on workspace switch (#247, 04#9)", async () => {
    const artifactsByWs: Record<string, ArtifactSummary[]> = {
      [WS_A]: [artifact("artifact-a1", "Alpha Dashboard")],
      [WS_B]: [artifact("artifact-b1", "Beta Dashboard")],
    }
    const getSpy = vi
      .spyOn(api, "get")
      .mockImplementation(async (url: string) => {
        const wsId = workspaceIdFromUrl(url)
        return { results: wsId ? artifactsByWs[wsId] : [] } as never
      })

    useAppStore.setState({
      activeDomainId: WS_A,
      artifacts: [],
      artifactsStatus: "idle",
      artifactSearch: "",
    })

    render(
      createElement(
        MemoryRouter,
        { initialEntries: ["/artifacts"] },
        createElement(
          Routes,
          null,
          createElement(Route, { path: "/artifacts", element: createElement(ArtifactsPage) }),
        ),
      ),
    )

    await waitFor(() => expect(screen.getByText("Alpha Dashboard")).toBeInTheDocument())
    expect(screen.queryByText("Beta Dashboard")).not.toBeInTheDocument()
    expect(getSpy).toHaveBeenCalledWith(`/api/workspaces/${WS_A}/artifacts/`)

    act(() => {
      useAppStore.getState().domainActions.setActiveDomain(WS_B)
    })

    await waitFor(() => expect(screen.getByText("Beta Dashboard")).toBeInTheDocument())
    expect(getSpy).toHaveBeenCalledWith(`/api/workspaces/${WS_B}/artifacts/`)
    expect(screen.queryByText("Alpha Dashboard")).not.toBeInTheDocument()
  })

  it("clears a prior workspace-detail load error on later success (#247, 05#5)", async () => {
    // WS_A fails to load (404); WS_B loads fine. Switching to the good one must
    // drop the stale error screen.
    const getDetailSpy = vi
      .spyOn(workspaceApi, "getDetail")
      .mockImplementation(async (id: string) => {
        if (id === WS_A) throw new ApiError(404, "Workspace not found")
        return workspaceDetail(WS_B, "Workspace Beta")
      })

    // Capture navigate in an effect (outside render) so the route param can be
    // driven imperatively without a lint-flagged side effect during render.
    let navigateTo: ((path: string) => void) | null = null
    function NavProbe() {
      const navigate = useNavigate()
      useEffect(() => {
        navigateTo = (path: string) => navigate(path)
      }, [navigate])
      return null
    }

    useAppStore.setState({ activeDomainId: WS_A, domains: [] })

    render(
      createElement(
        MemoryRouter,
        { initialEntries: [`/workspaces/${WS_A}`] },
        createElement(NavProbe),
        createElement(
          Routes,
          null,
          createElement(Route, {
            path: "/workspaces/:workspaceId",
            element: createElement(WorkspaceDetailPage),
          }),
        ),
      ),
    )

    await waitFor(() => expect(screen.getByText("Workspace not found")).toBeInTheDocument())
    expect(getDetailSpy).toHaveBeenCalledWith(WS_A)

    act(() => {
      navigateTo?.(`/workspaces/${WS_B}`)
    })

    // Successful load clears the prior error. (Old code never reset `error`, so
    // the stale error screen would persist → fail.)
    await waitFor(() => expect(screen.getByTestId("workspace-name")).toHaveTextContent("Workspace Beta"))
    expect(screen.queryByText("Workspace not found")).not.toBeInTheDocument()
    expect(getDetailSpy).toHaveBeenCalledWith(WS_B)
  })
})

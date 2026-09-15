import { act, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom"

import { api, ApiError } from "@/api/client"
import { WorkspaceSwitcher } from "@/components/WorkspaceSwitcher"
import { router } from "@/router"
import { useAppStore } from "@/store/store"
import type { TenantMembership } from "@/store/domainSlice"
import { ArtifactDetailPage } from "./ArtifactDetailPage"

const OWNER = "11111111-1111-1111-1111-111111111111"
const OTHER = "22222222-2222-2222-2222-222222222222"
const ARTIFACT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
const OTHER_ARTIFACT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
const PATH = `/workspaces/owner/${OWNER}/artifacts/${ARTIFACT}`
const DATA_URL = `/api/workspaces/${OWNER}/artifacts/${ARTIFACT}/data/`
const detail = {
  id: ARTIFACT,
  title: "Owning workspace artifact",
  type: "react",
  code: "",
  data: {},
  semantic_queries: [],
  version: 1,
}

function workspace(id: string, name: string): TenantMembership {
  return {
    id, name, display_name: name, is_auto_created: false, role: "read",
    tenants: [], member_count: 1, schema_status: "available",
    last_synced_at: null, created_at: "2026-01-01T00:00:00Z",
  }
}

function LocationProbe() {
  return <div data-testid="location">{useLocation().pathname}</div>
}

function RoutesUnderTest({ initialPath = PATH }: { initialPath?: string }) {
  return (
    <MemoryRouter initialEntries={[initialPath]}>
      <LocationProbe />
      <WorkspaceSwitcher />
      <Link to={`/workspaces/${OTHER}/artifacts/${OTHER_ARTIFACT}`}>Another artifact</Link>
      <Routes>
        <Route path="/artifacts" element={<div>Artifact list</div>} />
        <Route path="/artifacts/:artifactId" element={<ArtifactDetailPage />} />
        <Route path="/workspaces/:workspaceId/artifacts/:artifactId" element={<ArtifactDetailPage />} />
        <Route path="/workspaces/:slug/:workspaceId/artifacts/:artifactId" element={<ArtifactDetailPage />} />
      </Routes>
    </MemoryRouter>
  )
}

describe("workspace-qualified artifact links", () => {
  beforeEach(() => {
    localStorage.clear()
    useAppStore.setState({
      activeDomainId: OTHER,
      domains: [workspace(OWNER, "Owner"), workspace(OTHER, "Other")],
      domainsStatus: "loaded",
      activeArtifactId: null,
    })
  })

  afterEach(() => vi.restoreAllMocks())

  it("registers legacy, bare workspace, and pretty workspace detail routes", () => {
    const paths = router.routes.find((route) => route.path === "/")?.children?.map((route) => route.path)
    expect(paths).toEqual(expect.arrayContaining([
      "artifacts/:artifactId",
      "workspaces/:workspaceId/artifacts/:artifactId",
      "workspaces/:slug/:workspaceId/artifacts/:artifactId",
    ]))
  })

  it.each([
    PATH,
    `/workspaces/${OWNER}/artifacts/${ARTIFACT}`,
    `/workspaces/old-name/${OWNER}/artifacts/${ARTIFACT}`,
  ])("loads %s from its URL workspace, then adopts the authorized workspace", async (initialPath) => {
    const get = vi.spyOn(api, "get").mockResolvedValue(detail)
    render(<RoutesUnderTest initialPath={initialPath} />)

    await waitFor(() => expect(screen.getByTestId("artifact-detail-title")).toHaveTextContent(detail.title))
    expect(get).toHaveBeenCalledExactlyOnceWith(DATA_URL)
    await waitFor(() => expect(useAppStore.getState().activeDomainId).toBe(OWNER))
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(PATH))
    expect(screen.getByTestId(`artifact-frame-${ARTIFACT}`)).toHaveAttribute(
      "src", `/api/workspaces/${OWNER}/artifacts/${ARTIFACT}/sandbox/`,
    )
  })

  it("reopens a copied link after reload with a different default workspace", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(detail)
    const first = render(<RoutesUnderTest />)
    await waitFor(() => expect(useAppStore.getState().activeDomainId).toBe(OWNER))
    const copiedPath = screen.getByTestId("location").textContent!
    first.unmount()
    useAppStore.setState({ activeDomainId: OTHER })

    render(<RoutesUnderTest initialPath={copiedPath} />)
    await waitFor(() => expect(useAppStore.getState().activeDomainId).toBe(OWNER))
    expect(screen.getByTestId("artifact-detail-title")).toHaveTextContent(detail.title)
    expect(get.mock.calls.map(([url]) => url)).toEqual([DATA_URL, DATA_URL])
  })

  it("works before a workspace has been selected or the workspace list has loaded", async () => {
    useAppStore.setState({ activeDomainId: null, domains: [], domainsStatus: "loading" })
    const get = vi.spyOn(api, "get").mockResolvedValue(detail)
    render(<RoutesUnderTest />)

    await waitFor(() => expect(useAppStore.getState().activeDomainId).toBe(OWNER))
    expect(get).toHaveBeenCalledExactlyOnceWith(DATA_URL)
    expect(screen.getByTestId("artifact-detail-title")).toHaveTextContent(detail.title)
  })

  it("uses the URL workspace for recovery checks and query-data requests too", async () => {
    const get = vi.spyOn(api, "get").mockImplementation(async (url) => {
      if (url === DATA_URL) return { ...detail, semantic_queries: [{ name: "visits", measures: ["visits.count"] }] } as never
      if (url.endsWith("/recovery/")) return { status: "ready" } as never
      if (url.endsWith("/query-data/")) return { queries: [], static_data: {} } as never
      throw new Error(`Unexpected request: ${url}`)
    })
    render(<RoutesUnderTest />)
    await screen.findByTestId(`artifact-frame-${ARTIFACT}`)
    await userEvent.click(screen.getByTestId("artifact-view-data"))

    await waitFor(() => expect(get).toHaveBeenCalledWith(`/api/workspaces/${OWNER}/artifacts/${ARTIFACT}/query-data/`))
    expect(get).toHaveBeenCalledWith(`/api/workspaces/${OWNER}/artifacts/${ARTIFACT}/recovery/`)
    expect(get.mock.calls.every(([url]) => url.startsWith(`/api/workspaces/${OWNER}/`))).toBe(true)
  })

  it.each([403, 404])("keeps a scoped %s denial without trying the selected workspace", async (status) => {
    const get = vi.spyOn(api, "get").mockRejectedValue(new ApiError(status, "Artifact access denied"))
    render(<RoutesUnderTest />)

    expect(await screen.findByText("Artifact access denied")).toBeInTheDocument()
    expect(get).toHaveBeenCalledExactlyOnceWith(DATA_URL)
    expect(useAppStore.getState().activeDomainId).toBe(OTHER)
    expect(screen.queryByTestId(`artifact-frame-${ARTIFACT}`)).not.toBeInTheDocument()
  })

  it("qualifies an old artifact link only after a successful scoped lookup", async () => {
    useAppStore.setState({ activeDomainId: OWNER })
    vi.spyOn(api, "get").mockResolvedValue(detail)
    render(<RoutesUnderTest initialPath={`/artifacts/${ARTIFACT}`} />)

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(PATH))
  })

  it("leaves a failing legacy link selectable instead of guessing another workspace", async () => {
    const get = vi.spyOn(api, "get").mockRejectedValue(new ApiError(404, "Not found"))
    render(<RoutesUnderTest initialPath={`/artifacts/${ARTIFACT}`} />)

    expect(await screen.findByText("Not found")).toBeInTheDocument()
    expect(screen.getByTestId("location")).toHaveTextContent(`/artifacts/${ARTIFACT}`)
    expect(get).toHaveBeenCalledExactlyOnceWith(`/api/workspaces/${OTHER}/artifacts/${ARTIFACT}/data/`)
  })

  it("switches to the artifact list without looking up the old artifact in the new workspace", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(detail)
    render(<RoutesUnderTest />)
    await waitFor(() => expect(useAppStore.getState().activeDomainId).toBe(OWNER))

    await userEvent.click(screen.getByTestId("domain-selector"))
    await userEvent.type(screen.getByTestId("workspace-search"), "Other")
    await userEvent.click(screen.getByTestId(`domain-item-${OTHER}`))

    expect(await screen.findByText("Artifact list")).toBeInTheDocument()
    expect(screen.getByTestId("location")).toHaveTextContent("/artifacts")
    expect(useAppStore.getState().activeDomainId).toBe(OTHER)
    expect(get).toHaveBeenCalledExactlyOnceWith(DATA_URL)
  })

  it("also leaves the detail page when another action switches the selected workspace", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(detail)
    render(<RoutesUnderTest />)
    await waitFor(() => expect(useAppStore.getState().activeDomainId).toBe(OWNER))

    act(() => useAppStore.getState().domainActions.setActiveDomain(OTHER))

    expect(await screen.findByText("Artifact list")).toBeInTheDocument()
    expect(get).toHaveBeenCalledExactlyOnceWith(DATA_URL)
  })

  it("ignores a slow old artifact after navigating to a different workspace's link", async () => {
    let finishOld!: (value: typeof detail) => void
    vi.spyOn(api, "get")
      .mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve }))
      .mockResolvedValueOnce({ ...detail, id: OTHER_ARTIFACT, title: "New artifact" })
    render(<RoutesUnderTest />)

    await userEvent.click(screen.getByRole("link", { name: "Another artifact" }))
    await waitFor(() => expect(screen.getByTestId("artifact-detail-title")).toHaveTextContent("New artifact"))
    await act(async () => finishOld(detail))

    expect(screen.getByTestId("artifact-detail-title")).toHaveTextContent("New artifact")
    expect(useAppStore.getState().activeDomainId).toBe(OTHER)
    expect(screen.getByTestId("location")).toHaveTextContent(`/workspaces/other/${OTHER}/artifacts/${OTHER_ARTIFACT}`)
  })
})

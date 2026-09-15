import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { createMemoryRouter, RouterProvider } from "react-router-dom"
import { api } from "@/api/client"
import { authApi } from "@/api/auth"
import { workspaceApi, type WorkspaceDetail } from "@/api/workspaces"
import { clearUserTenantsCache } from "@/api/userTenantsCache"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal/CreateWorkspaceModal"
import { getRecentWorkspaceIds } from "@/lib/recentWorkspaces"
import { KnowledgePage } from "@/pages/KnowledgePage/KnowledgePage"
import { RecipeRunner } from "@/pages/RecipesPage/RecipeRunner"
import { RecipesPage } from "@/pages/RecipesPage/RecipesPage"
import { WorkspaceDetailPage } from "@/pages/WorkspaceDetailPage/WorkspaceDetailPage"
import { useAppStore } from "@/store/store"
import type { Recipe, RecipeRun } from "@/store/recipeSlice"

vi.mock("@/hooks/useNetworkStatus", () => ({ useNetworkStatus: () => ({ status: "online" }) }))

const user = (id: string) => ({
  id, email: `${id}@example.invalid`, name: id, is_staff: false, onboarding_complete: true,
})
const workspace = (id: string) => ({
  id, name: id, display_name: id, has_access: true, is_auto_created: false,
  role: "manage" as const, tenants: [], member_count: 1, schema_status: "available" as const,
  last_synced_at: null, created_at: "2026-01-01T00:00:00Z",
})
const RECIPE: Recipe = {
  id: "recipe-a", name: "Synthetic recipe", description: "", prompt: "", variables: [],
  is_shared: false, created_at: "", updated_at: "",
}
const RUN: RecipeRun = {
  id: "run-a", status: "pending", variable_values: {}, step_results: [], is_shared: false,
  is_public: false, share_token: null, started_at: null, completed_at: null, created_at: "",
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

function AccountPage({ children }: { children: ReactNode }) {
  const id = useAppStore((state) => state.user?.id)
  return id === "user-a" ? children : <div>Account B</div>
}

function renderAccountRoute(element: ReactNode, initialEntry = "/workspaces", path = "*") {
  const router = createMemoryRouter(
    [
      { path, element: <AccountPage>{element}</AccountPage> },
      ...(path === "*" ? [] : [{ path: "*", element: <div>Other route</div> }]),
    ],
    { initialEntries: [initialEntry] },
  )
  function AuthBoundary() {
    const id = useAppStore((state) => state.user?.id)
    // Match App/EmbedApp: the router itself survives the keyed provider remount.
    return <RouterProvider key={id} router={router} />
  }
  render(<AuthBoundary />)
  return router
}

function switchToB() {
  act(() => {
    useAppStore.setState({ user: user("user-b"), authStatus: "authenticated" })
    useAppStore.setState({
      domains: [workspace("workspace-b")], activeDomainId: "workspace-b", domainsStatus: "loaded",
    })
  })
}

beforeEach(() => {
  localStorage.clear()
  clearUserTenantsCache()
  useAppStore.setState({ user: user("user-a"), authStatus: "authenticated" })
  useAppStore.setState({
    domains: [workspace("workspace-a")], activeDomainId: "workspace-a", domainsStatus: "loaded",
  })
  vi.spyOn(authApi, "getUserTenants").mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  useAppStore.setState({ user: null, authStatus: "idle" })
  vi.restoreAllMocks()
})

describe("delayed account-owned navigation", () => {
  it.each(["create", "refresh"])("does not navigate B when A's workspace %s finishes", async (phase) => {
    const pending = deferred<ReturnType<typeof workspace>>()
    const refresh = deferred<ReturnType<typeof workspace>[]>()
    vi.spyOn(workspaceApi, "create").mockReturnValue(pending.promise)
    vi.spyOn(workspaceApi, "list").mockReturnValue(refresh.promise)
    const onClose = vi.fn()
    const router = renderAccountRoute(<CreateWorkspaceModal onClose={onClose} />)
    const input = screen.getByTestId("workspace-name-input")
    fireEvent.change(input, { target: { value: "Synthetic private workspace A" } })
    fireEvent.submit(input.closest("form")!)
    expect(workspaceApi.create).toHaveBeenCalledOnce()

    if (phase === "refresh") {
      await act(async () => { pending.resolve(workspace("private-a-created")); await pending.promise })
      expect(workspaceApi.list).toHaveBeenCalledOnce()
    }
    switchToB()
    expect(screen.queryByTestId("create-workspace-modal")).toBeNull()
    const beforeRecents = getRecentWorkspaceIds()
    await act(async () => {
      pending.resolve(workspace("private-a-created"))
      refresh.resolve([workspace("private-a-created")])
      await pending.promise
      await refresh.promise
    })

    expect(useAppStore.getState().activeDomainId).toBe("workspace-b")
    expect(router.state.location.pathname).toBe("/workspaces")
    expect(getRecentWorkspaceIds()).toEqual(beforeRecents)
    expect(onClose).not.toHaveBeenCalled()
    if (phase === "create") expect(workspaceApi.list).not.toHaveBeenCalled()
  })

  it("still selects and opens a newly created workspace in the current account", async () => {
    vi.spyOn(workspaceApi, "create").mockResolvedValue(workspace("created-a"))
    vi.spyOn(workspaceApi, "list").mockResolvedValue([workspace("created-a")])
    const onClose = vi.fn()
    const router = renderAccountRoute(<CreateWorkspaceModal onClose={onClose} />)
    const input = screen.getByTestId("workspace-name-input")
    fireEvent.change(input, { target: { value: "Synthetic current workspace" } })
    fireEvent.submit(input.closest("form")!)

    await waitFor(() => expect(router.state.location.pathname).toBe("/workspaces/created-a/created-a"))
    expect(useAppStore.getState().activeDomainId).toBe("created-a")
    expect(onClose).toHaveBeenCalledOnce()
  })

  it("does not invoke recipe completion navigation after account A unmounts", async () => {
    const pending = deferred<RecipeRun>()
    const onRun = vi.fn(() => pending.promise)
    const onOpenChange = vi.fn()
    const onRunComplete = vi.fn()
    renderAccountRoute(
      <RecipeRunner open recipe={RECIPE} onRun={onRun} onOpenChange={onOpenChange} onRunComplete={onRunComplete} />,
    )
    fireEvent.click(screen.getByRole("button", { name: "Run Recipe" }))
    expect(onRun).toHaveBeenCalledOnce()
    switchToB()
    await act(async () => { pending.resolve(RUN); await pending.promise })

    expect(onOpenChange).not.toHaveBeenCalled()
    expect(onRunComplete).not.toHaveBeenCalled()
  })

  it("still completes a recipe in the current account", async () => {
    const onOpenChange = vi.fn()
    const onRunComplete = vi.fn()
    renderAccountRoute(
      <RecipeRunner open recipe={RECIPE} onRun={vi.fn().mockResolvedValue(RUN)} onOpenChange={onOpenChange} onRunComplete={onRunComplete} />,
    )
    fireEvent.click(screen.getByRole("button", { name: "Run Recipe" }))

    await waitFor(() => expect(onRunComplete).toHaveBeenCalledWith(RECIPE.id, RUN.id))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it("does not navigate B when A's knowledge form save finishes", async () => {
    const pending = deferred<unknown>()
    vi.spyOn(api, "get").mockResolvedValue({ results: [], pagination: null })
    vi.spyOn(api, "post").mockReturnValue(pending.promise)
    const router = renderAccountRoute(<KnowledgePage />, "/knowledge/new")
    const title = await screen.findByLabelText("Title")
    fireEvent.change(title, { target: { value: "Synthetic knowledge A" } })
    fireEvent.change(screen.getByLabelText("Content"), { target: { value: "Private synthetic content" } })
    fireEvent.submit(title.closest("form")!)
    expect(api.post).toHaveBeenCalledOnce()
    switchToB()
    await act(async () => {
      pending.resolve({ id: "entry-a", type: "entry", title: "Synthetic knowledge A" })
      await pending.promise
    })

    expect(router.state.location.pathname).toBe("/knowledge/new")
    expect(useAppStore.getState().knowledgeItems).toEqual([])
  })

  it("does not navigate B when A's recipe deletion finishes", async () => {
    const pending = deferred<void>()
    const detail = deferred<Recipe>()
    vi.spyOn(api, "get").mockImplementation((url) => {
      if (url.endsWith("/recipes/")) return Promise.resolve([RECIPE])
      if (url.endsWith("/runs/")) return Promise.resolve([])
      return detail.promise
    })
    vi.spyOn(api, "delete").mockReturnValue(pending.promise)
    const initialEntry = "/recipes/recipe-a"
    const router = renderAccountRoute(<RecipesPage />, initialEntry, "/recipes/:id")
    // The list remains available while the detail is still loading.
    const card = await screen.findByTestId("recipe-card-recipe-a")
    fireEvent.click(within(card).getAllByRole("button")[2])
    fireEvent.click(screen.getByRole("button", { name: "Delete" }))
    expect(api.delete).toHaveBeenCalledOnce()
    switchToB()
    await act(async () => {
      pending.resolve()
      detail.resolve(RECIPE)
      await pending.promise
      await detail.promise
    })

    expect(router.state.location.pathname).toBe(initialEntry)
    expect(useAppStore.getState().recipes).toEqual([])
  })

  it("does not navigate B when A's workspace deletion finishes", async () => {
    const pending = deferred<void>()
    const detail: WorkspaceDetail = {
      ...workspace("workspace-a"), system_prompt: "", tenant_count: 0, updated_at: "",
    }
    vi.spyOn(workspaceApi, "getDetail").mockResolvedValue(detail)
    vi.spyOn(workspaceApi, "getMembers").mockResolvedValue({ members: [], invites: [] })
    vi.spyOn(workspaceApi, "delete").mockReturnValue(pending.promise)
    vi.spyOn(workspaceApi, "list").mockResolvedValue([workspace("workspace-b")])
    const initialEntry = "/workspaces/workspace-a/workspace-a"
    const router = renderAccountRoute(<WorkspaceDetailPage />, initialEntry, "/workspaces/:slug/:workspaceId")
    const settings = await screen.findByTestId("tab-settings")
    fireEvent.mouseDown(settings, { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByTestId("delete-workspace-btn"))
    fireEvent.click(screen.getByTestId("confirm-delete-workspace-btn"))
    expect(workspaceApi.delete).toHaveBeenCalledOnce()
    switchToB()
    await act(async () => { pending.resolve(); await pending.promise })

    expect(router.state.location.pathname).toBe(initialEntry)
    expect(workspaceApi.list).not.toHaveBeenCalled()
  })
})

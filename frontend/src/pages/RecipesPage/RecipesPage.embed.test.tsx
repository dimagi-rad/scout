import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { createMemoryRouter, RouterProvider } from "react-router-dom"
import { api } from "@/api/client"
import { useAppStore } from "@/store/store"
import type { Recipe, RecipeRun } from "@/store/recipeSlice"
import { RecipesPage } from "./RecipesPage"

vi.mock("@/hooks/useNetworkStatus", () => ({ useNetworkStatus: () => ({ status: "online" }) }))

const RECIPE: Recipe = {
  id: "recipe-a", name: "Synthetic recipe", description: "", prompt: "", variables: [],
  created_at: "", updated_at: "",
}
const RUN: RecipeRun = {
  id: "run-a", status: "completed", variable_values: {}, step_results: [],
  started_at: null, completed_at: null, created_at: "",
}

function renderAt(mount: string, initialEntry: string) {
  const router = createMemoryRouter(
    [
      { path: `${mount}/recipes`, element: <RecipesPage /> },
      { path: `${mount}/recipes/:id`, element: <RecipesPage /> },
      { path: `${mount}/recipes/:id/runs/:runId`, element: <RecipesPage /> },
      { path: "*", element: <div>Outside the mount</div> },
    ],
    { initialEntries: [initialEntry] },
  )
  render(<RouterProvider router={router} />)
  return router
}

beforeEach(() => {
  useAppStore.setState({
    user: {
      id: "user-a", email: "user-a@example.invalid", name: "user-a", is_staff: false,
      onboarding_complete: true,
    },
    authStatus: "authenticated",
  })
  // Separate update: a user change resets account-owned state such as the workspace.
  useAppStore.setState({
    domains: [{
      id: "workspace-a", name: "workspace-a", display_name: "workspace-a", has_access: true,
      is_auto_created: false, role: "manage", tenants: [], member_count: 1,
      schema_status: "available", last_synced_at: null, created_at: "2026-01-01T00:00:00Z",
    }],
    activeDomainId: "workspace-a",
    domainsStatus: "loaded",
  })
  vi.spyOn(api, "get").mockImplementation((url: string) => {
    if (url.endsWith("/recipes/")) return Promise.resolve([RECIPE])
    if (url.endsWith("/runs/")) return Promise.resolve([RUN])
    return Promise.resolve(RECIPE)
  })
})

afterEach(() => {
  cleanup()
  useAppStore.setState({ user: null, authStatus: "idle" })
  vi.restoreAllMocks()
})

describe.each(["", "/embed"])("recipe navigation mounted at '%s'", (mount) => {
  const path = (rest: string) => `${mount}/recipes${rest}`

  it("keeps list → recipe → run → back inside the mount", async () => {
    const router = renderAt(mount, path(""))

    fireEvent.click(await screen.findByTestId("recipe-view-recipe-a"))
    expect(router.state.location.pathname).toBe(path("/recipe-a"))

    fireEvent.click(await screen.findByText("Run History"))
    fireEvent.click(await screen.findByTestId("recipe-run-view-run-a"))
    expect(router.state.location.pathname).toBe(path("/recipe-a/runs/run-a"))

    fireEvent.click(await screen.findByTestId("run-detail-back"))
    expect(router.state.location.pathname).toBe(path("/recipe-a"))

    await waitFor(() => expect(screen.getByTestId("recipe-detail-run")).toBeInTheDocument())
    fireEvent.click(screen.getByRole("button", { name: /back/i }))
    expect(router.state.location.pathname).toBe(path(""))
    expect(screen.queryByText("Outside the mount")).not.toBeInTheDocument()
  })
})

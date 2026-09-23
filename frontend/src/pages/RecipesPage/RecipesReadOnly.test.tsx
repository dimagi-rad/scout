import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { READ_ONLY_HINT } from "@/hooks/useWorkspaceRole"
import type { Recipe } from "@/store/recipeSlice"
import { RecipeDetail } from "./RecipeDetail"
import { RecipesList } from "./RecipesList"

const recipe: Recipe = {
  id: "recipe-1",
  name: "Weekly visits",
  description: "Visits per week",
  prompt: "Summarise visits for {{week}}",
  variables: [],
  is_shared: true,
  created_at: "2026-09-01T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
}

function renderDetail(canWrite: boolean) {
  render(
    <RecipeDetail
      recipe={recipe}
      runs={[]}
      onBack={vi.fn()}
      onSave={vi.fn()}
      onRun={vi.fn()}
      onUpdateRun={vi.fn()}
      onViewRun={vi.fn()}
      canWrite={canWrite}
    />,
  )
}

describe("recipes for read-only members", () => {
  it("keeps Run and View but hides Delete", () => {
    render(
      <RecipesList
        recipes={[recipe]}
        onView={vi.fn()}
        onRun={vi.fn()}
        onDelete={vi.fn()}
        canWrite={false}
      />,
    )

    expect(screen.getByTestId("recipe-run-recipe-1")).toBeEnabled()
    expect(screen.getByTestId("recipe-view-recipe-1")).toBeEnabled()
    expect(screen.queryByTestId("recipe-delete-recipe-1")).not.toBeInTheDocument()
  })

  it("shows the recipe read-only with Run still available", () => {
    renderDetail(false)

    expect(screen.queryByTestId("recipe-save")).not.toBeInTheDocument()
    expect(screen.getByTestId("recipe-readonly-hint")).toHaveTextContent(READ_ONLY_HINT)
    expect(screen.getByTestId("recipe-prompt-editor")).toHaveAttribute("readonly")
    expect(screen.getByLabelText("Name")).toHaveAttribute("readonly")
    expect(screen.getByRole("checkbox")).toBeDisabled()
    expect(screen.getByTestId("recipe-detail-run")).toBeEnabled()
  })

  it("leaves editing untouched for writers", () => {
    renderDetail(true)

    expect(screen.getByTestId("recipe-save")).toBeInTheDocument()
    expect(screen.getByTestId("recipe-prompt-editor")).not.toHaveAttribute("readonly")
    expect(screen.getByRole("checkbox")).toBeEnabled()
  })
})

import { useEffect, useState, useCallback } from "react"
import { useNavigate, useParams } from "react-router-dom"
import { useAppStore } from "@/store/store"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { RecipesList } from "./RecipesList"
import { RecipeDetail } from "./RecipeDetail"
import { RecipeRunner } from "./RecipeRunner"
import { RecipeRunDetail } from "./RecipeRunDetail"
import type { Recipe } from "@/store/recipeSlice"

export function RecipesPage() {
  const { id, runId } = useParams<{ id: string; runId: string }>()
  const navigate = useNavigate()

  const recipes = useAppStore((s) => s.recipes)
  const recipeStatus = useAppStore((s) => s.recipeStatus)
  const currentRecipe = useAppStore((s) => s.currentRecipe)
  const recipeRuns = useAppStore((s) => s.recipeRuns)
  const {
    fetchRecipes,
    fetchRecipe,
    updateRecipe,
    deleteRecipe,
    runRecipe,
    fetchRuns,
    updateRecipeRun,
  } = useAppStore((s) => s.recipeActions)

  const [runnerOpen, setRunnerOpen] = useState(false)
  const [runnerRecipe, setRunnerRecipe] = useState<Recipe | null>(null)
  const [deleteDialogRecipe, setDeleteDialogRecipe] = useState<Recipe | null>(null)

  // Fetch recipes list on mount
  useEffect(() => {
    fetchRecipes()
  }, [fetchRecipes])

  // Fetch specific recipe when viewing detail
  useEffect(() => {
    if (id) {
      fetchRecipe(id)
      fetchRuns(id)
    }
  }, [id, fetchRecipe, fetchRuns])

  const handleView = useCallback(
    (recipe: Recipe) => {
      navigate(`/recipes/${recipe.id}`)
    },
    [navigate]
  )

  const handleBack = useCallback(() => {
    navigate("/recipes")
  }, [navigate])

  const handleRun = useCallback(
    async (recipe: Recipe) => {
      try {
        const full = await fetchRecipe(recipe.id)
        setRunnerRecipe(full)
        setRunnerOpen(true)
      } catch {
        setRunnerRecipe(recipe)
        setRunnerOpen(true)
      }
    },
    [fetchRecipe]
  )

  const handleRunFromDetail = useCallback(() => {
    if (currentRecipe) {
      setRunnerRecipe(currentRecipe)
      setRunnerOpen(true)
    }
  }, [currentRecipe])

  const handleBackFromRun = useCallback(() => {
    navigate(`/recipes/${id}`)
  }, [navigate, id])

  const handleViewRun = useCallback(
    (runId: string) => {
      navigate(`/recipes/${id}/runs/${runId}`)
    },
    [navigate, id],
  )

  const handleDelete = useCallback((recipe: Recipe) => {
    setDeleteDialogRecipe(recipe)
  }, [])

  const handleConfirmDelete = useCallback(async () => {
    if (!deleteDialogRecipe) return

    await deleteRecipe(deleteDialogRecipe.id)
    setDeleteDialogRecipe(null)

    // If we're on the detail page of the deleted recipe, go back to list
    if (id === deleteDialogRecipe.id) {
      navigate("/recipes")
    }
  }, [deleteDialogRecipe, deleteRecipe, id, navigate])

  const handleSave = useCallback(
    async (data: Partial<Recipe>) => {
      if (!currentRecipe) return
      await updateRecipe(currentRecipe.id, data)
    },
    [currentRecipe, updateRecipe]
  )

  const handleUpdateRun = useCallback(
    async (runId: string, data: { is_shared?: boolean; is_public?: boolean }) => {
      if (!currentRecipe) return
      await updateRecipeRun(currentRecipe.id, runId, data)
    },
    [currentRecipe, updateRecipeRun]
  )

  const handleExecuteRun = useCallback(
    async (variables: Record<string, string>) => {
      if (!runnerRecipe) {
        throw new Error("No recipe selected")
      }
      return await runRecipe(runnerRecipe.id, variables)
    },
    [runnerRecipe, runRecipe]
  )

  const handleRunComplete = useCallback(
    (recipeId: string, runId: string) => {
      navigate(`/recipes/${recipeId}/runs/${runId}`)
    },
    [navigate],
  )

  // Show run detail view if we have both recipe ID and run ID
  if (id && runId && currentRecipe) {
    const run = recipeRuns.find((r) => r.id === runId)
    if (run) {
      return (
        <div className="container mx-auto py-8">
          <RecipeRunDetail
            recipe={currentRecipe}
            run={run}
            onBack={handleBackFromRun}
            onUpdateRun={handleUpdateRun}
          />
        </div>
      )
    }
  }

  // Show detail view if we have an ID
  if (id && currentRecipe) {
    return (
      <div className="container mx-auto py-8">
        <RecipeDetail
          recipe={currentRecipe}
          runs={recipeRuns}
          onBack={handleBack}
          onSave={handleSave}
          onRun={handleRunFromDetail}
          onUpdateRun={handleUpdateRun}
          onViewRun={handleViewRun}
        />

        <RecipeRunner
          open={runnerOpen}
          onOpenChange={setRunnerOpen}
          recipe={runnerRecipe}
          onRun={handleExecuteRun}
          onRunComplete={handleRunComplete}
        />

        <AlertDialog
          open={!!deleteDialogRecipe}
          onOpenChange={() => setDeleteDialogRecipe(null)}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete Recipe</AlertDialogTitle>
              <AlertDialogDescription>
                Are you sure you want to delete "{deleteDialogRecipe?.name}"? This
                action cannot be undone.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={handleConfirmDelete}>
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    )
  }

  // Show list view
  return (
    <div className="container mx-auto py-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl font-bold">Recipes</h1>
        <p className="text-muted-foreground">
          Manage and run automated workflows created by the AI agent
        </p>
      </div>

      {/* Loading state */}
      {recipeStatus === "loading" && (
        <div className="text-muted-foreground">Loading recipes...</div>
      )}

      {/* Error state */}
      {recipeStatus === "error" && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
          Failed to load recipes. Please try again.
        </div>
      )}

      {/* List */}
      {recipeStatus === "loaded" && (
        <RecipesList
          recipes={recipes}
          onView={handleView}
          onRun={handleRun}
          onDelete={handleDelete}
        />
      )}

      {/* Runner Dialog */}
      <RecipeRunner
        open={runnerOpen}
        onOpenChange={setRunnerOpen}
        recipe={runnerRecipe}
        onRun={handleExecuteRun}
        onRunComplete={handleRunComplete}
      />

      {/* Delete Confirmation Dialog */}
      <AlertDialog
        open={!!deleteDialogRecipe}
        onOpenChange={() => setDeleteDialogRecipe(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Recipe</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete "{deleteDialogRecipe?.name}"? This
              action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmDelete}>
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

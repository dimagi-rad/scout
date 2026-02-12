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
import type { Recipe } from "@/store/recipeSlice"

export function RecipesPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()

  const activeProjectId = useAppStore((s) => s.activeProjectId)
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

  // Fetch recipes list on mount and when project changes
  useEffect(() => {
    if (activeProjectId) {
      fetchRecipes(activeProjectId)
    }
  }, [activeProjectId, fetchRecipes])

  // Fetch specific recipe when viewing detail
  useEffect(() => {
    if (activeProjectId && id) {
      fetchRecipe(activeProjectId, id)
      fetchRuns(activeProjectId, id)
    }
  }, [activeProjectId, id, fetchRecipe, fetchRuns])

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
      if (!activeProjectId) return
      // Fetch full recipe details (list view doesn't include variables/steps)
      try {
        const full = await fetchRecipe(activeProjectId, recipe.id)
        setRunnerRecipe(full)
        setRunnerOpen(true)
      } catch {
        setRunnerRecipe(recipe)
        setRunnerOpen(true)
      }
    },
    [activeProjectId, fetchRecipe]
  )

  const handleRunFromDetail = useCallback(() => {
    if (currentRecipe) {
      setRunnerRecipe(currentRecipe)
      setRunnerOpen(true)
    }
  }, [currentRecipe])

  const handleDelete = useCallback((recipe: Recipe) => {
    setDeleteDialogRecipe(recipe)
  }, [])

  const handleConfirmDelete = useCallback(async () => {
    if (!activeProjectId || !deleteDialogRecipe) return

    await deleteRecipe(activeProjectId, deleteDialogRecipe.id)
    setDeleteDialogRecipe(null)

    // If we're on the detail page of the deleted recipe, go back to list
    if (id === deleteDialogRecipe.id) {
      navigate("/recipes")
    }
  }, [activeProjectId, deleteDialogRecipe, deleteRecipe, id, navigate])

  const handleSave = useCallback(
    async (data: Partial<Recipe>) => {
      if (!activeProjectId || !currentRecipe) return
      await updateRecipe(activeProjectId, currentRecipe.id, data)
    },
    [activeProjectId, currentRecipe, updateRecipe]
  )

  const handleUpdateRun = useCallback(
    async (runId: string, data: { is_shared?: boolean; is_public?: boolean }) => {
      if (!activeProjectId || !currentRecipe) return
      await updateRecipeRun(activeProjectId, currentRecipe.id, runId, data)
    },
    [activeProjectId, currentRecipe, updateRecipeRun]
  )

  const handleExecuteRun = useCallback(
    async (variables: Record<string, string>) => {
      if (!activeProjectId || !runnerRecipe) {
        throw new Error("No project or recipe selected")
      }
      return await runRecipe(activeProjectId, runnerRecipe.id, variables)
    },
    [activeProjectId, runnerRecipe, runRecipe]
  )

  if (!activeProjectId) {
    return (
      <div className="container mx-auto py-8">
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-muted-foreground">Please select a project first</p>
        </div>
      </div>
    )
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
        />

        <RecipeRunner
          open={runnerOpen}
          onOpenChange={setRunnerOpen}
          recipe={runnerRecipe}
          onRun={handleExecuteRun}
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

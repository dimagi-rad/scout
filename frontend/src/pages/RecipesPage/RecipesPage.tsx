import { useEffect, useState, useCallback, type ReactNode } from "react"
import { useNavigate, useParams } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { useNetworkStatus } from "@/hooks/useNetworkStatus"
import { useIsCurrentAccount } from "@/hooks/useIsCurrentAccount"
import { useWorkspaceRole, writeErrorMessage } from "@/hooks/useWorkspaceRole"
import { Button } from "@/components/ui/button"
import {
  AlertDialog,
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
  const isCurrentAccount = useIsCurrentAccount()

  const activeDomainId = useAppStore((s) => s.activeDomainId)
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

  const { status: networkStatus } = useNetworkStatus()
  const { canWrite } = useWorkspaceRole()
  const [runnerOpen, setRunnerOpen] = useState(false)
  const [runnerRecipe, setRunnerRecipe] = useState<Recipe | null>(null)
  const [deleteDialogRecipe, setDeleteDialogRecipe] = useState<Recipe | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)

  // Refetch on workspace change so the previous workspace's recipes don't
  // linger (they then 404 against the new workspace id).
  useEffect(() => {
    if (!activeDomainId) return
    fetchRecipes()
  }, [activeDomainId, fetchRecipes])

  useEffect(() => {
    if (!activeDomainId || !id) return
    void fetchRecipe(id).catch(() => undefined)
    void fetchRuns(id)
  }, [activeDomainId, id, fetchRecipe, fetchRuns])

  // Recipe execution is a background task (POST /run/ returns 202 with a PENDING
  // run), so poll the runs list until a terminal status. Depending on the status
  // string (not the array identity) keeps this to one interval.
  const viewedRunStatus =
    id && runId ? recipeRuns.find((r) => r.id === runId)?.status : undefined
  useEffect(() => {
    if (!id || !runId) return
    if (viewedRunStatus !== "pending" && viewedRunStatus !== "running") return
    let interval: ReturnType<typeof setInterval> | null = null
    // Pause the run-status poll while the tab is hidden (arch #254, 05#6).
    const start = () => {
      if (interval !== null) return
      interval = setInterval(() => void fetchRuns(id), 2000)
    }
    const stop = () => {
      if (interval !== null) {
        clearInterval(interval)
        interval = null
      }
    }
    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        void fetchRuns(id)
        start()
      } else {
        stop()
      }
    }
    if (document.visibilityState === "visible") start()
    document.addEventListener("visibilitychange", handleVisibility)
    return () => {
      stop()
      document.removeEventListener("visibilitychange", handleVisibility)
    }
  }, [id, runId, viewedRunStatus, fetchRuns])

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
    setDeleteError(null)
    setDeleteDialogRecipe(recipe)
  }, [])

  const handleConfirmDelete = useCallback(async () => {
    if (!deleteDialogRecipe || isDeleting) return

    setIsDeleting(true)
    try {
      await deleteRecipe(deleteDialogRecipe.id)
    } catch (error) {
      if (!isCurrentAccount()) return
      setDeleteError(writeErrorMessage(error, "Couldn’t delete this recipe. Try again.", canWrite))
      return
    } finally {
      if (isCurrentAccount()) setIsDeleting(false)
    }
    if (!isCurrentAccount()) return
    setDeleteDialogRecipe(null)

    if (id === deleteDialogRecipe.id) {
      navigate("/recipes")
    }
  }, [deleteDialogRecipe, isDeleting, deleteRecipe, id, navigate, isCurrentAccount, canWrite])

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

  let body: ReactNode = null

  if (id && runId && currentRecipe) {
    const run = recipeRuns.find((r) => r.id === runId)
    if (run) {
      body = (
        <div className="container mx-auto px-8 py-8">
          <RecipeRunDetail
            key={run.id}
            recipe={currentRecipe}
            run={run}
            onBack={handleBackFromRun}
            onUpdateRun={handleUpdateRun}
            canWrite={canWrite}
          />
        </div>
      )
    }
  }

  if (!body && id && currentRecipe) {
    body = (
      <div className="container mx-auto px-8 py-8">
        <RecipeDetail
          recipe={currentRecipe}
          runs={recipeRuns}
          onBack={handleBack}
          onSave={handleSave}
          onRun={handleRunFromDetail}
          onUpdateRun={handleUpdateRun}
          onViewRun={handleViewRun}
          canWrite={canWrite}
        />

        <RecipeRunner
          open={runnerOpen}
          onOpenChange={setRunnerOpen}
          recipe={runnerRecipe}
          onRun={handleExecuteRun}
          onRunComplete={handleRunComplete}
        />
      </div>
    )
  }

  body ??= (
    <div className="container mx-auto px-8 py-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold">Recipes</h1>
        <p className="text-muted-foreground">
          Manage and run automated workflows created by the AI agent
        </p>
      </div>

      {recipeStatus === "loading" && (
        <div className="text-muted-foreground">Loading recipes...</div>
      )}

      {recipeStatus === "error" && networkStatus === "online" && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
          Failed to load recipes. Please try again.
        </div>
      )}

      {recipeStatus === "loaded" && (
        <RecipesList
          recipes={recipes}
          onView={handleView}
          onRun={handleRun}
          onDelete={handleDelete}
          canWrite={canWrite}
        />
      )}

      <RecipeRunner
        open={runnerOpen}
        onOpenChange={setRunnerOpen}
        recipe={runnerRecipe}
        onRun={handleExecuteRun}
        onRunComplete={handleRunComplete}
      />
    </div>
  )

  // One dialog outside the branches: the list also renders at /recipes/:id
  // until the detail loads, and a branch switch must not unmount it.
  return (
    <>
      {body}
      <AlertDialog
        open={!!deleteDialogRecipe}
        onOpenChange={(open) => !open && !isDeleting && setDeleteDialogRecipe(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Recipe</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete "{deleteDialogRecipe?.name}"? This
              action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteError && (
            <p className="text-sm text-destructive" role="alert">
              {deleteError}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              onClick={handleConfirmDelete}
              disabled={isDeleting}
              data-testid="recipe-confirm-delete"
            >
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

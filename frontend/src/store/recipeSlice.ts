import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export interface RecipeVariable {
  name: string
  type: "string" | "number" | "date" | "boolean" | "select"
  required: boolean
  default?: string
  options?: string[] // for select type
}

export interface Recipe {
  id: string
  name: string
  description: string
  prompt: string
  variables: RecipeVariable[]
  is_shared: boolean
  variable_count?: number
  last_run_at?: string
  created_at: string
  updated_at: string
}

export interface StepResult {
  step_order: number
  prompt: string
  response: string
  tools_used: string[]
  artifacts_created: string[]
  success: boolean
  error: string | null
  started_at: string
  completed_at: string | null
}

export interface RecipeRun {
  id: string
  status: "pending" | "running" | "completed" | "failed"
  variable_values: Record<string, string>
  step_results: StepResult[]
  is_shared: boolean
  is_public: boolean
  share_token: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string
}

export type RecipeStatus = "idle" | "loading" | "loaded" | "error"

export interface RecipeSlice {
  recipes: Recipe[]
  recipeStatus: RecipeStatus
  recipeError: string | null
  currentRecipe: Recipe | null
  recipeRuns: RecipeRun[]
  recipeActions: {
    fetchRecipes: () => Promise<void>
    fetchRecipe: (recipeId: string) => Promise<Recipe>
    updateRecipe: (recipeId: string, data: Partial<Recipe>) => Promise<Recipe>
    deleteRecipe: (recipeId: string) => Promise<void>
    runRecipe: (recipeId: string, variables: Record<string, string>) => Promise<RecipeRun>
    fetchRuns: (recipeId: string) => Promise<void>
    updateRecipeRun: (
      recipeId: string,
      runId: string,
      data: { is_shared?: boolean; is_public?: boolean },
    ) => Promise<RecipeRun>
  }
}

export const createRecipeSlice: StateCreator<RecipeSlice, [], [], RecipeSlice> = (set, get) => ({
  recipes: [],
  recipeStatus: "idle",
  recipeError: null,
  currentRecipe: null,
  recipeRuns: [],
  recipeActions: {
    fetchRecipes: async () => {
      set({ recipeStatus: "loading", recipeError: null })
      try {
        const recipes = await api.get<Recipe[]>(`/api/recipes/`)
        set({ recipes, recipeStatus: "loaded", recipeError: null })
      } catch (error) {
        set({
          recipeStatus: "error",
          recipeError: error instanceof Error ? error.message : "Failed to load recipes",
        })
      }
    },

    fetchRecipe: async (recipeId: string) => {
      try {
        const recipe = await api.get<Recipe>(`/api/recipes/${recipeId}/`)
        set({ currentRecipe: recipe })
        return recipe
      } catch (error) {
        set({ currentRecipe: null })
        throw error
      }
    },

    updateRecipe: async (recipeId: string, data: Partial<Recipe>) => {
      const recipe = await api.put<Recipe>(`/api/recipes/${recipeId}/`, data)
      const recipes = get().recipes.map((r) => (r.id === recipeId ? recipe : r))
      set({
        recipes,
        currentRecipe: get().currentRecipe?.id === recipeId ? recipe : get().currentRecipe,
      })
      return recipe
    },

    deleteRecipe: async (recipeId: string) => {
      await api.delete<void>(`/api/recipes/${recipeId}/`)
      const recipes = get().recipes.filter((r) => r.id !== recipeId)
      set({
        recipes,
        currentRecipe: get().currentRecipe?.id === recipeId ? null : get().currentRecipe,
      })
    },

    runRecipe: async (recipeId: string, variables: Record<string, string>) => {
      const run = await api.post<RecipeRun>(`/api/recipes/${recipeId}/run/`, {
        variable_values: variables,
      })
      const runs = get().recipeRuns
      set({ recipeRuns: [run, ...runs] })
      return run
    },

    fetchRuns: async (recipeId: string) => {
      try {
        const runs = await api.get<RecipeRun[]>(`/api/recipes/${recipeId}/runs/`)
        set({ recipeRuns: runs })
      } catch {
        set({ recipeRuns: [] })
      }
    },

    updateRecipeRun: async (
      recipeId: string,
      runId: string,
      data: { is_shared?: boolean; is_public?: boolean },
    ) => {
      const updated = await api.patch<RecipeRun>(
        `/api/recipes/${recipeId}/runs/${runId}/`,
        data,
      )
      const runs = get().recipeRuns.map((r) => (r.id === runId ? updated : r))
      set({ recipeRuns: runs })
      return updated
    },
  },
})

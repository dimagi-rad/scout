import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export interface RecipeVariable {
  name: string
  type: "string" | "number" | "date" | "boolean" | "select"
  required: boolean
  default?: string
  options?: string[] // for select type
}

export interface RecipeStep {
  id: string
  order: number
  prompt_template: string
}

export interface Recipe {
  id: string
  name: string
  description: string
  variables: RecipeVariable[]
  steps: RecipeStep[]
  step_count?: number
  variable_count?: number
  last_run_at?: string
  created_at: string
  updated_at: string
}

export interface RecipeRun {
  id: string
  status: "pending" | "running" | "completed" | "failed"
  variable_values: Record<string, string>
  step_results: unknown[]
  started_at: string | null
  completed_at: string | null
  created_at: string
}

export type RecipeStatus = "idle" | "loading" | "loaded" | "error"

export interface RecipeSlice {
  recipes: Recipe[]
  recipeStatus: RecipeStatus
  currentRecipe: Recipe | null
  recipeRuns: RecipeRun[]
  recipeActions: {
    fetchRecipes: (projectId: string) => Promise<void>
    fetchRecipe: (projectId: string, recipeId: string) => Promise<Recipe>
    updateRecipe: (projectId: string, recipeId: string, data: Partial<Recipe>) => Promise<Recipe>
    deleteRecipe: (projectId: string, recipeId: string) => Promise<void>
    runRecipe: (projectId: string, recipeId: string, variables: Record<string, string>) => Promise<RecipeRun>
    fetchRuns: (projectId: string, recipeId: string) => Promise<void>
  }
}

export const createRecipeSlice: StateCreator<RecipeSlice, [], [], RecipeSlice> = (set, get) => ({
  recipes: [],
  recipeStatus: "idle",
  currentRecipe: null,
  recipeRuns: [],
  recipeActions: {
    fetchRecipes: async (projectId: string) => {
      set({ recipeStatus: "loading" })
      try {
        const recipes = await api.get<Recipe[]>(`/api/projects/${projectId}/recipes/`)
        set({ recipes, recipeStatus: "loaded" })
      } catch {
        set({ recipeStatus: "error" })
      }
    },

    fetchRecipe: async (projectId: string, recipeId: string) => {
      try {
        const recipe = await api.get<Recipe>(`/api/projects/${projectId}/recipes/${recipeId}/`)
        set({ currentRecipe: recipe })
        return recipe
      } catch (error) {
        set({ currentRecipe: null })
        throw error
      }
    },

    updateRecipe: async (projectId: string, recipeId: string, data: Partial<Recipe>) => {
      const recipe = await api.put<Recipe>(`/api/projects/${projectId}/recipes/${recipeId}/`, data)
      const recipes = get().recipes.map((r) => (r.id === recipeId ? recipe : r))
      set({
        recipes,
        currentRecipe: get().currentRecipe?.id === recipeId ? recipe : get().currentRecipe,
      })
      return recipe
    },

    deleteRecipe: async (projectId: string, recipeId: string) => {
      await api.delete<void>(`/api/projects/${projectId}/recipes/${recipeId}/`)
      const recipes = get().recipes.filter((r) => r.id !== recipeId)
      set({
        recipes,
        currentRecipe: get().currentRecipe?.id === recipeId ? null : get().currentRecipe,
      })
    },

    runRecipe: async (projectId: string, recipeId: string, variables: Record<string, string>) => {
      const run = await api.post<RecipeRun>(`/api/projects/${projectId}/recipes/${recipeId}/run/`, {
        variable_values: variables,
      })
      const runs = get().recipeRuns
      set({ recipeRuns: [run, ...runs] })
      return run
    },

    fetchRuns: async (projectId: string, recipeId: string) => {
      try {
        const runs = await api.get<RecipeRun[]>(`/api/projects/${projectId}/recipes/${recipeId}/runs/`)
        set({ recipeRuns: runs })
      } catch {
        set({ recipeRuns: [] })
      }
    },
  },
})

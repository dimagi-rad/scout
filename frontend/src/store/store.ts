import { create } from "zustand"
import { createArtifactSlice, type ArtifactSlice } from "./artifactSlice"
import { createAuthSlice, type AuthSlice } from "./authSlice"
import { createProjectSlice, type ProjectSlice } from "./projectSlice"
import { createUiSlice, type UiSlice } from "./uiSlice"
import { createDictionarySlice, type DictionarySlice } from "./dictionarySlice"
import { createKnowledgeSlice, type KnowledgeSlice } from "./knowledgeSlice"
import { createRecipeSlice, type RecipeSlice } from "./recipeSlice"

export type AppStore = ArtifactSlice & AuthSlice & ProjectSlice & UiSlice & DictionarySlice & KnowledgeSlice & RecipeSlice

export const useAppStore = create<AppStore>()((...a) => ({
  ...createArtifactSlice(...a),
  ...createAuthSlice(...a),
  ...createProjectSlice(...a),
  ...createUiSlice(...a),
  ...createDictionarySlice(...a),
  ...createKnowledgeSlice(...a),
  ...createRecipeSlice(...a),
}))

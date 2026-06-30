import { create } from "zustand"
import { createArtifactSlice, type ArtifactSlice } from "./artifactSlice"
import { createAuthSlice, type AuthSlice } from "./authSlice"
import { createUiSlice, type UiSlice } from "./uiSlice"
import { createDictionarySlice, type DictionarySlice } from "./dictionarySlice"
import { createDatasetSlice, type DatasetSlice } from "./datasetSlice"
import { createKnowledgeSlice, type KnowledgeSlice } from "./knowledgeSlice"
import { createRecipeSlice, type RecipeSlice } from "./recipeSlice"
import { createDomainSlice, type DomainSlice } from "./domainSlice"

export type AppStore = ArtifactSlice & AuthSlice & UiSlice & DictionarySlice & DatasetSlice & KnowledgeSlice & RecipeSlice & DomainSlice

export const useAppStore = create<AppStore>()((...a) => ({
  ...createArtifactSlice(...a),
  ...createAuthSlice(...a),
  ...createUiSlice(...a),
  ...createDictionarySlice(...a),
  ...createDatasetSlice(...a),
  ...createKnowledgeSlice(...a),
  ...createRecipeSlice(...a),
  ...createDomainSlice(...a),
}))

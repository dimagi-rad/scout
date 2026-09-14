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

export function createAppStore() {
  const store = create<AppStore>()((...a) => ({
    ...createArtifactSlice(...a),
    ...createAuthSlice(...a),
    ...createUiSlice(...a),
    ...createDictionarySlice(...a),
    ...createDatasetSlice(...a),
    ...createKnowledgeSlice(...a),
    ...createRecipeSlice(...a),
    ...createDomainSlice(...a),
  }))
  store.subscribe((state, previous) => {
    if (state.activeDomainId === previous.activeDomainId) return
    // Clear before UI subscribers run; generations also reject A→B→A responses.
    store.setState({
      workspaceGeneration: state.workspaceGeneration + 1,
      artifacts: [], artifactsStatus: "idle", artifactsError: null, artifactSearch: "",
      activeArtifactId: null,
      dataDictionary: null, dictionaryStatus: "idle", dictionaryError: null, selectedTable: null,
      threads: [], threadsStatus: "idle", threadsAccessLostMessage: null,
      datasetCatalog: null, datasetStatus: "idle", datasetError: null,
      selectedDataset: null, selectedDatasetStatus: "idle", selectedDatasetError: null,
      recipes: [], recipeStatus: "idle", recipeError: null, currentRecipe: null, recipeRuns: [],
      knowledgeItems: [], knowledgeStatus: "idle", knowledgeError: null, knowledgePagination: null,
      knowledgeFilter: null, knowledgeSearch: "",
    })
  })
  return store
}

export const useAppStore = createAppStore()

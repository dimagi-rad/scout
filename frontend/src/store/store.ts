import { create, type StoreApi } from "zustand"
import { createArtifactSlice, type ArtifactSlice } from "./artifactSlice"
import { createAuthSlice, type AuthSlice } from "./authSlice"
import { createUiSlice, type UiSlice } from "./uiSlice"
import { createDictionarySlice, type DictionarySlice } from "./dictionarySlice"
import { createDatasetSlice, type DatasetSlice } from "./datasetSlice"
import { createKnowledgeSlice, type KnowledgeSlice } from "./knowledgeSlice"
import { createRecipeSlice, type RecipeSlice } from "./recipeSlice"
import { createDomainSlice, type DomainSlice } from "./domainSlice"
import type { AccountSessionScope } from "./accountSession"

export type AppStore = ArtifactSlice & AuthSlice & UiSlice & DictionarySlice & DatasetSlice & KnowledgeSlice & RecipeSlice & DomainSlice & AccountSessionScope

export function createAppStore() {
  let session: { snapshot: AppStore | null } | null = null

  function accountState(set: StoreApi<AppStore>["setState"], get: () => AppStore, api: StoreApi<AppStore>) {
    const owner = { snapshot: null as AppStore | null }
    session = owner
    const scopedSet: typeof set = (...args) => {
      if (session === owner) Reflect.apply(set, undefined, args)
    }
    // Old continuations must not read or mutate the next account's objects,
    // nor obtain its fresh actions when starting a delayed follow-up.
    const scopedGet = () => owner.snapshot ?? get()
    const args = [scopedSet, scopedGet, { ...api, setState: scopedSet, getState: scopedGet }] as const
    return {
      accountSession: { isCurrent: () => session === owner },
      ...createArtifactSlice(...args),
      ...createUiSlice(...args),
      ...createDictionarySlice(...args),
      ...createDatasetSlice(...args),
      ...createKnowledgeSlice(...args),
      ...createRecipeSlice(...args),
      ...createDomainSlice(...args),
    }
  }

  const store = create<AppStore>()((set, get, api) => ({
    ...createAuthSlice(set, get, api),
    ...accountState(set, get, api),
  }))
  store.subscribe((state, previous) => {
    if (state.user?.id === previous.user?.id) return
    if (session) session.snapshot = previous
    // Recreate the slices so even A → logout → A cannot revive old responses.
    // Seed account-owned fields only after setting the identity.
    store.setState(accountState(store.setState, store.getState, store))
  })
  store.subscribe((state, previous) => {
    if (state.activeDomainId === previous.activeDomainId) return
    // Clear before UI subscribers run; seed workspace-owned state after selecting a workspace.
    store.setState({
      workspaceGeneration: state.workspaceGeneration + 1,
      threadId: crypto.randomUUID(),
      artifacts: [], artifactsStatus: "idle", artifactsError: null, artifactSearch: "",
      activeArtifactId: null,
      dataDictionary: null, dictionaryStatus: "idle", dictionaryError: null, selectedTable: null,
      threads: [], threadsStatus: "idle", threadsAccessLostMessage: null,
      threadsAccessRetryable: false,
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

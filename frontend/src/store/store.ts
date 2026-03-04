import { create } from "zustand"
import { createArtifactSlice, type ArtifactSlice } from "./artifactSlice"
import { createAuthSlice, type AuthSlice } from "./authSlice"
import { createUiSlice, type UiSlice } from "./uiSlice"
import { createDictionarySlice, type DictionarySlice } from "./dictionarySlice"
import { createKnowledgeSlice, type KnowledgeSlice } from "./knowledgeSlice"
import { createRecipeSlice, type RecipeSlice } from "./recipeSlice"
import { createDomainSlice, type DomainSlice } from "./domainSlice"
import { createWorkspaceSlice, type WorkspaceSlice } from "./workspaceSlice"

export type AppStore = ArtifactSlice & AuthSlice & UiSlice & DictionarySlice & KnowledgeSlice & RecipeSlice & DomainSlice & WorkspaceSlice

export const useAppStore = create<AppStore>()((...a) => ({
  ...createArtifactSlice(...a),
  ...createAuthSlice(...a),
  ...createUiSlice(...a),
  ...createDictionarySlice(...a),
  ...createKnowledgeSlice(...a),
  ...createRecipeSlice(...a),
  ...createDomainSlice(...a),
  ...createWorkspaceSlice(...a),
}))

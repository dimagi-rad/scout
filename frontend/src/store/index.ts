// Re-export store
export { useAppStore, type AppStore } from "./store"

// Re-export slice types for convenience
export type { AuthSlice } from "./authSlice"
export type { UiSlice, Thread, ThreadShareState } from "./uiSlice"
export type {
  Column,
  TableAnnotations,
  TableInfo,
  DataDictionary,
  TableDetail,
  DictionaryStatus,
  DictionarySlice,
} from "./dictionarySlice"
export type {
  DatasetCatalog,
  DatasetDetailStatus,
  DatasetSlice,
  DatasetStatus,
  SemanticDataset,
  SemanticField,
  SemanticModelSummary,
  SemanticRelationship,
} from "./datasetSlice"
export type {
  KnowledgeType,
  KnowledgeItem,
  KnowledgeEntryItem,
  LearningItem,
  PaginationInfo,
  KnowledgeStatus,
  KnowledgeSlice,
} from "./knowledgeSlice"
export { getKnowledgeItemName } from "./knowledgeSlice"
export type {
  RecipeVariable,
  Recipe,
  RecipeRun,
  RecipeStatus,
  RecipeSlice,
} from "./recipeSlice"

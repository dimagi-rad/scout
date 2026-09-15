import type { StateCreator } from "zustand"
import { api, ApiError } from "@/api/client"
import type { DomainSlice } from "./domainSlice"
import { createWorkspaceRequestGuard } from "./workspaceRequest"

export interface SemanticField {
  id: string
  name: string
  member: string
  label: string
  description: string
  type: "dimension" | "time_dimension" | "measure"
  data_type: string
  measure_type: string
  metadata: Record<string, unknown>
}

export interface SemanticRelationship {
  id: string
  name: string
  from_dataset: string
  to_dataset: string
  relationship_type: string
  join_expression: string
  direction?: "outgoing" | "incoming"
}

export interface SemanticDataset {
  id: string
  name: string
  label: string
  description: string
  source_kind: "physical" | "custom"
  definition_sql: string
  schema_name: string
  table_name: string
  primary_key: string
  row_count: number | null
  row_count_verified: boolean
  dimensions: SemanticField[]
  time_dimensions: SemanticField[]
  measures: SemanticField[]
  relationships: SemanticRelationship[]
  metadata: Record<string, unknown>
}

export interface SemanticModelSummary {
  id: string
  name: string
  version: number
  status: string
  diagnostics: Array<Record<string, unknown>>
  updated_at: string
}

export interface DatasetCatalog {
  model: SemanticModelSummary
  datasets: SemanticDataset[]
}

interface DatasetDetailResponse {
  model: SemanticModelSummary
  dataset: SemanticDataset
}

export type DatasetStatus = "idle" | "loading" | "loaded" | "error" | "not_materialized"
export type DatasetDetailStatus = "idle" | "loading" | "loaded" | "error"

export interface DatasetSlice {
  datasetCatalog: DatasetCatalog | null
  datasetStatus: DatasetStatus
  datasetError: string | null
  selectedDataset: SemanticDataset | null
  selectedDatasetStatus: DatasetDetailStatus
  selectedDatasetError: string | null
  datasetActions: {
    fetchDatasets: () => Promise<void>
    fetchDataset: (datasetName: string) => Promise<void>
    clearDatasets: () => void
  }
}

export const createDatasetSlice: StateCreator<
  DatasetSlice & DomainSlice,
  [],
  [],
  DatasetSlice
> = (set, get) => {
  const requests = createWorkspaceRequestGuard(get)
  return {
    datasetCatalog: null,
    datasetStatus: "idle",
    datasetError: null,
    selectedDataset: null,
    selectedDatasetStatus: "idle",
    selectedDatasetError: null,
    datasetActions: {
      fetchDatasets: async () => {
        const isCurrent = requests.start("catalog")
        const isSelectionCurrent = requests.start("selection")
        set({ datasetStatus: "loading", datasetError: null, selectedDatasetError: null })
        try {
          const activeDomainId = get().activeDomainId
          if (!activeDomainId) throw new Error("No active workspace selected.")
          const catalog = await api.get<DatasetCatalog>(
            `/api/workspaces/${activeDomainId}/datasets/`
          )
          if (!isCurrent()) return
          set({
            datasetCatalog: catalog,
            ...(isSelectionCurrent() ? {
              selectedDataset: catalog.datasets[0] ?? null,
              selectedDatasetStatus: catalog.datasets[0] ? "loaded" as const : "idle" as const,
              selectedDatasetError: null,
            } : {}),
            datasetStatus: "loaded",
            datasetError: null,
          })
        } catch (error) {
          if (!isCurrent()) return
          const status =
            error instanceof ApiError && error.status === 503 ? "not_materialized" : "error"
          set({
            datasetStatus: status,
            datasetError: error instanceof Error ? error.message : "Failed to load datasets",
            ...(isSelectionCurrent() ? {
              selectedDatasetStatus: "idle" as const,
              selectedDatasetError: null,
            } : {}),
          })
        }
      },

      fetchDataset: async (datasetName: string) => {
        const isCurrent = requests.start("selection")
        const activeDomainId = get().activeDomainId
        if (!activeDomainId) throw new Error("No active workspace selected.")
        const cachedDataset = get().datasetCatalog?.datasets.find(
          (dataset) => dataset.name === datasetName
        )
        set({ selectedDatasetStatus: "loading", selectedDatasetError: null })
        if (cachedDataset) {
          set({ selectedDataset: cachedDataset })
        }
        try {
          const raw = await api.get<DatasetDetailResponse>(
            `/api/workspaces/${activeDomainId}/datasets/${datasetName}/`
          )
          if (!isCurrent()) return
          set({
            selectedDataset: raw.dataset,
            selectedDatasetStatus: "loaded",
            selectedDatasetError: null,
          })
        } catch (error) {
          if (!isCurrent()) return
          set({
            selectedDataset: cachedDataset ?? null,
            selectedDatasetStatus: "error",
            selectedDatasetError:
              error instanceof Error ? error.message : "Failed to load dataset details",
          })
        }
      },

      clearDatasets: () => {
        requests.invalidate()
        set({
          datasetCatalog: null,
          datasetStatus: "idle",
          datasetError: null,
          selectedDataset: null,
          selectedDatasetStatus: "idle",
          selectedDatasetError: null,
        })
      },
    },
  }
}

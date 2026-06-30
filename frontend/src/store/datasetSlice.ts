import type { StateCreator } from "zustand"
import { api, ApiError } from "@/api/client"
import type { DomainSlice } from "./domainSlice"

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
}

export interface SemanticDataset {
  id: string
  name: string
  label: string
  description: string
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

export interface DatasetSlice {
  datasetCatalog: DatasetCatalog | null
  datasetStatus: DatasetStatus
  datasetError: string | null
  selectedDataset: SemanticDataset | null
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
> = (set, get) => ({
  datasetCatalog: null,
  datasetStatus: "idle",
  datasetError: null,
  selectedDataset: null,
  datasetActions: {
    fetchDatasets: async () => {
      set({ datasetStatus: "loading", datasetError: null })
      try {
        const activeDomainId = get().activeDomainId
        if (!activeDomainId) throw new Error("No active workspace selected.")
        const catalog = await api.get<DatasetCatalog>(
          `/api/workspaces/${activeDomainId}/datasets/`
        )
        set({
          datasetCatalog: catalog,
          selectedDataset: catalog.datasets[0] ?? null,
          datasetStatus: "loaded",
          datasetError: null,
        })
      } catch (error) {
        const status =
          error instanceof ApiError && error.status === 503 ? "not_materialized" : "error"
        set({
          datasetStatus: status,
          datasetError: error instanceof Error ? error.message : "Failed to load datasets",
        })
      }
    },

    fetchDataset: async (datasetName: string) => {
      const activeDomainId = get().activeDomainId
      if (!activeDomainId) throw new Error("No active workspace selected.")
      const raw = await api.get<DatasetDetailResponse>(
        `/api/workspaces/${activeDomainId}/datasets/${datasetName}/`
      )
      set({ selectedDataset: raw.dataset })
    },

    clearDatasets: () => {
      set({
        datasetCatalog: null,
        datasetStatus: "idle",
        datasetError: null,
        selectedDataset: null,
      })
    },
  },
})

import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export interface Column {
  name: string
  type: string
  nullable: boolean
  default: string | null
  description?: string
}

export interface TableAnnotations {
  description: string
  use_cases: string
  data_quality_notes: string
  refresh_frequency: string
  owner: string
  related_tables: string[]
  column_notes: Record<string, string>
}

export interface TableInfo {
  columns: Column[]
  annotations?: TableAnnotations
}

export interface DataDictionary {
  schemas: Record<string, Record<string, TableInfo>>
}

export interface TableDetail {
  schema: string
  table: string
  columns: Column[]
  annotations: TableAnnotations | null
}

export type DictionaryStatus = "idle" | "loading" | "loaded" | "error"

export interface DictionarySlice {
  dataDictionary: DataDictionary | null
  dictionaryStatus: DictionaryStatus
  selectedTable: TableDetail | null
  dictionaryActions: {
    fetchDictionary: (projectId: string) => Promise<void>
    refreshSchema: (projectId: string) => Promise<void>
    fetchTable: (projectId: string, schema: string, table: string) => Promise<void>
    updateAnnotations: (
      projectId: string,
      schema: string,
      table: string,
      annotations: Partial<TableAnnotations>
    ) => Promise<void>
    clearDictionary: () => void
  }
}

export const createDictionarySlice: StateCreator<
  DictionarySlice,
  [],
  [],
  DictionarySlice
> = (set, get) => ({
  dataDictionary: null,
  dictionaryStatus: "idle",
  selectedTable: null,
  dictionaryActions: {
    fetchDictionary: async (projectId: string) => {
      set({ dictionaryStatus: "loading" })
      try {
        const data = await api.get<DataDictionary>(
          `/api/projects/${projectId}/data-dictionary/`
        )
        set({ dataDictionary: data, dictionaryStatus: "loaded" })
      } catch {
        set({ dictionaryStatus: "error" })
      }
    },

    refreshSchema: async (projectId: string) => {
      set({ dictionaryStatus: "loading" })
      try {
        const data = await api.post<DataDictionary>(
          `/api/projects/${projectId}/refresh-schema/`
        )
        set({ dataDictionary: data, dictionaryStatus: "loaded" })
      } catch {
        set({ dictionaryStatus: "error" })
      }
    },

    fetchTable: async (projectId: string, schema: string, table: string) => {
      const data = await api.get<TableDetail>(
        `/api/projects/${projectId}/data-dictionary/tables/${schema}.${table}/`
      )
      set({ selectedTable: data })
    },

    updateAnnotations: async (
      projectId: string,
      schema: string,
      table: string,
      annotations: Partial<TableAnnotations>
    ) => {
      const updated = await api.put<TableAnnotations>(
        `/api/projects/${projectId}/data-dictionary/tables/${schema}.${table}/`,
        annotations
      )
      // Update selected table
      const current = get().selectedTable
      if (current && current.schema === schema && current.table === table) {
        set({ selectedTable: { ...current, annotations: updated } })
      }
      // Update in dictionary cache
      const dict = get().dataDictionary
      if (dict?.schemas?.[schema]?.[table]) {
        dict.schemas[schema][table].annotations = updated
        set({ dataDictionary: { ...dict } })
      }
    },

    clearDictionary: () => {
      set({ dataDictionary: null, dictionaryStatus: "idle", selectedTable: null })
    },
  },
})

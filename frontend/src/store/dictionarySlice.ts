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

/** Shape of the data dictionary as returned by the backend API. */
interface BackendColumn {
  name: string
  data_type: string
  nullable: boolean
  default: string | null
  primary_key?: boolean
  foreign_key?: Record<string, string>
}

interface BackendAnnotation {
  description: string
  use_cases: string
  data_quality_notes: string
  refresh_frequency: string
  owner: string
  related_tables: string[]
  column_notes: Record<string, string>
}

interface BackendTable {
  schema: string
  name: string
  type: string
  columns: BackendColumn[]
  primary_key: string[]
  annotation?: BackendAnnotation
}

interface BackendDictionaryResponse {
  tables: Record<string, BackendTable>
  generated_at: string | null
}

interface BackendTableDetailResponse {
  schema: string
  name: string
  qualified_name: string
  type: string
  columns: BackendColumn[]
  primary_key: string[]
  annotation?: BackendAnnotation
}

export type DictionaryStatus = "idle" | "loading" | "loaded" | "error"

export interface DictionarySlice {
  dataDictionary: DataDictionary | null
  dictionaryStatus: DictionaryStatus
  dictionaryError: string | null
  selectedTable: TableDetail | null
  dictionaryActions: {
    fetchDictionary: () => Promise<void>
    refreshSchema: () => Promise<void>
    fetchTable: (schema: string, table: string) => Promise<void>
    updateAnnotations: (
      schema: string,
      table: string,
      annotations: Partial<TableAnnotations>
    ) => Promise<void>
    clearDictionary: () => void
  }
}

/** Transform the flat backend response into the nested schema structure the UI expects. */
function transformBackendResponse(raw: BackendDictionaryResponse): DataDictionary {
  const schemas: Record<string, Record<string, TableInfo>> = {}

  for (const [_qualifiedName, table] of Object.entries(raw.tables || {})) {
    const schemaName = table.schema || "public"
    const tableName = table.name || _qualifiedName

    if (!schemas[schemaName]) {
      schemas[schemaName] = {}
    }

    schemas[schemaName][tableName] = {
      columns: (table.columns || []).map((col) => ({
        name: col.name,
        type: col.data_type,
        nullable: col.nullable,
        default: col.default ?? null,
      })),
      annotations: table.annotation
        ? {
            description: table.annotation.description,
            use_cases: table.annotation.use_cases,
            data_quality_notes: table.annotation.data_quality_notes,
            refresh_frequency: table.annotation.refresh_frequency,
            owner: table.annotation.owner,
            related_tables: table.annotation.related_tables,
            column_notes: table.annotation.column_notes,
          }
        : undefined,
    }
  }

  return { schemas }
}

export const createDictionarySlice: StateCreator<
  DictionarySlice,
  [],
  [],
  DictionarySlice
> = (set, get) => ({
  dataDictionary: null,
  dictionaryStatus: "idle",
  dictionaryError: null,
  selectedTable: null,
  dictionaryActions: {
    fetchDictionary: async () => {
      set({ dictionaryStatus: "loading", dictionaryError: null })
      try {
        const raw = await api.get<BackendDictionaryResponse>(
          `/api/data-dictionary/`
        )
        const data = transformBackendResponse(raw)
        set({ dataDictionary: data, dictionaryStatus: "loaded", dictionaryError: null })
      } catch (error) {
        set({
          dictionaryStatus: "error",
          dictionaryError: error instanceof Error ? error.message : "Failed to load data dictionary",
        })
      }
    },

    refreshSchema: async () => {
      set({ dictionaryStatus: "loading", dictionaryError: null })
      try {
        await api.post(`/api/refresh-schema/`)
        // Re-fetch the full dictionary after refresh
        const raw = await api.get<BackendDictionaryResponse>(
          `/api/data-dictionary/`
        )
        const data = transformBackendResponse(raw)
        set({ dataDictionary: data, dictionaryStatus: "loaded", dictionaryError: null })
      } catch (error) {
        set({
          dictionaryStatus: "error",
          dictionaryError: error instanceof Error ? error.message : "Failed to refresh schema",
        })
      }
    },

    fetchTable: async (schema: string, table: string) => {
      const raw = await api.get<BackendTableDetailResponse>(
        `/api/data-dictionary/tables/${schema}.${table}/`
      )
      const data: TableDetail = {
        schema: raw.schema,
        table: raw.name,
        columns: (raw.columns || []).map((col) => ({
          name: col.name,
          type: col.data_type,
          nullable: col.nullable,
          default: col.default ?? null,
        })),
        annotations: raw.annotation
          ? {
              description: raw.annotation.description,
              use_cases: raw.annotation.use_cases,
              data_quality_notes: raw.annotation.data_quality_notes,
              refresh_frequency: raw.annotation.refresh_frequency,
              owner: raw.annotation.owner,
              related_tables: raw.annotation.related_tables,
              column_notes: raw.annotation.column_notes,
            }
          : null,
      }
      set({ selectedTable: data })
    },

    updateAnnotations: async (
      schema: string,
      table: string,
      annotations: Partial<TableAnnotations>
    ) => {
      const updated = await api.put<TableAnnotations>(
        `/api/data-dictionary/tables/${schema}.${table}/`,
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

import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export type KnowledgeType = "entry" | "learning"

/**
 * KnowledgeEntry - type: "entry"
 */
export interface KnowledgeEntryItem {
  id: string
  type: "entry"
  title: string
  content: string
  tags: string[]
  created_at: string
  updated_at: string
}

/**
 * AgentLearning - type: "learning"
 */
export interface LearningItem {
  id: string
  type: "learning"
  description: string
  category?: string
  applies_to_tables?: string[]
  original_error?: string
  original_sql?: string
  corrected_sql?: string
  confidence_score?: number
  times_applied?: number
  is_active?: boolean
  created_at: string
}

/**
 * Union type for all knowledge items
 */
export type KnowledgeItem = KnowledgeEntryItem | LearningItem

/**
 * Helper to get display name for any knowledge item
 */
export function getKnowledgeItemName(item: KnowledgeItem): string {
  switch (item.type) {
    case "entry":
      return item.title
    case "learning":
      return item.description.slice(0, 50) + (item.description.length > 50 ? "..." : "")
  }
}

/**
 * Pagination metadata from the API
 */
export interface PaginationInfo {
  page: number
  page_size: number
  total_count: number
  total_pages: number
  has_next: boolean
  has_previous: boolean
}

interface PaginatedKnowledgeResponse {
  results: KnowledgeItem[]
  pagination: PaginationInfo
}

export type KnowledgeStatus = "idle" | "loading" | "loaded" | "error"

export interface KnowledgeSlice {
  knowledgeItems: KnowledgeItem[]
  knowledgeStatus: KnowledgeStatus
  knowledgeError: string | null
  knowledgePagination: PaginationInfo | null
  knowledgeFilter: KnowledgeType | null
  knowledgeSearch: string
  knowledgeActions: {
    fetchKnowledge: (projectId: string, options?: { type?: KnowledgeType; search?: string; page?: number; pageSize?: number }) => Promise<void>
    createKnowledge: (projectId: string, data: Partial<KnowledgeItem> & { type: KnowledgeType }) => Promise<KnowledgeItem>
    updateKnowledge: (projectId: string, id: string, data: Partial<KnowledgeItem>) => Promise<KnowledgeItem>
    deleteKnowledge: (projectId: string, id: string) => Promise<void>
    exportKnowledge: (projectId: string) => Promise<void>
    importKnowledge: (projectId: string, file: File) => Promise<void>
    setFilter: (type: KnowledgeType | null) => void
    setSearch: (search: string) => void
  }
}

export const createKnowledgeSlice: StateCreator<KnowledgeSlice, [], [], KnowledgeSlice> = (set, get) => ({
  knowledgeItems: [],
  knowledgeStatus: "idle",
  knowledgeError: null,
  knowledgePagination: null,
  knowledgeFilter: null,
  knowledgeSearch: "",
  knowledgeActions: {
    fetchKnowledge: async (projectId: string, options?: { type?: KnowledgeType; search?: string; page?: number; pageSize?: number }) => {
      set({ knowledgeStatus: "loading", knowledgeError: null })
      try {
        const params = new URLSearchParams()
        if (options?.type) params.set("type", options.type)
        if (options?.search) params.set("search", options.search)
        if (options?.page) params.set("page", String(options.page))
        if (options?.pageSize) params.set("page_size", String(options.pageSize))
        const queryString = params.toString()
        const url = `/api/projects/${projectId}/knowledge/${queryString ? `?${queryString}` : ""}`
        const response = await api.get<PaginatedKnowledgeResponse>(url)
        set({
          knowledgeItems: response.results,
          knowledgePagination: response.pagination,
          knowledgeStatus: "loaded",
          knowledgeError: null,
        })
      } catch (error) {
        set({
          knowledgeStatus: "error",
          knowledgePagination: null,
          knowledgeError: error instanceof Error ? error.message : "Failed to load knowledge items",
        })
      }
    },

    createKnowledge: async (projectId: string, data: Partial<KnowledgeItem> & { type: KnowledgeType }) => {
      const item = await api.post<KnowledgeItem>(`/api/projects/${projectId}/knowledge/`, data)
      const items = get().knowledgeItems
      set({ knowledgeItems: [item, ...items] })
      return item
    },

    updateKnowledge: async (projectId: string, id: string, data: Partial<KnowledgeItem>) => {
      const item = await api.put<KnowledgeItem>(`/api/projects/${projectId}/knowledge/${id}/`, data)
      const items = get().knowledgeItems.map((i) => (i.id === id ? item : i))
      set({ knowledgeItems: items })
      return item
    },

    deleteKnowledge: async (projectId: string, id: string) => {
      await api.delete<void>(`/api/projects/${projectId}/knowledge/${id}/`)
      const items = get().knowledgeItems.filter((i) => i.id !== id)
      set({ knowledgeItems: items })
    },

    exportKnowledge: async (projectId: string) => {
      const blob = await api.getBlob(`/api/projects/${projectId}/knowledge/export/`)
      const url = URL.createObjectURL(blob)
      const a = document.createElement("a")
      a.href = url
      a.download = `knowledge-export.zip`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    },

    importKnowledge: async (projectId: string, file: File) => {
      const formData = new FormData()
      formData.append("file", file)
      await api.upload(`/api/projects/${projectId}/knowledge/import/`, formData)
      // Re-fetch to get updated list
      await get().knowledgeActions.fetchKnowledge(projectId)
    },

    setFilter: (type: KnowledgeType | null) => {
      set({ knowledgeFilter: type })
    },

    setSearch: (search: string) => {
      set({ knowledgeSearch: search })
    },
  },
})

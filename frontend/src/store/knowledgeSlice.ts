import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export type KnowledgeType = "metric" | "rule" | "query" | "learning"

export interface KnowledgeItem {
  id: string
  type: KnowledgeType
  name?: string
  description?: string
  sql_template?: string
  rule_text?: string
  context?: string
  sql?: string
  tags?: string[]
  correction?: string
  confidence?: number
  promoted_to?: string
  related_tables: string[]
  created_at: string
  updated_at: string
}

export type KnowledgeStatus = "idle" | "loading" | "loaded" | "error"

export interface KnowledgeSlice {
  knowledgeItems: KnowledgeItem[]
  knowledgeStatus: KnowledgeStatus
  knowledgeFilter: KnowledgeType | null
  knowledgeSearch: string
  knowledgeActions: {
    fetchKnowledge: (projectId: string, type?: KnowledgeType, search?: string) => Promise<void>
    createKnowledge: (projectId: string, data: Partial<KnowledgeItem> & { type: KnowledgeType }) => Promise<KnowledgeItem>
    updateKnowledge: (projectId: string, id: string, data: Partial<KnowledgeItem>) => Promise<KnowledgeItem>
    deleteKnowledge: (projectId: string, id: string) => Promise<void>
    promoteKnowledge: (projectId: string, id: string, data: { target_type: "rule" | "query"; name: string; [key: string]: unknown }) => Promise<KnowledgeItem>
    setFilter: (type: KnowledgeType | null) => void
    setSearch: (search: string) => void
  }
}

export const createKnowledgeSlice: StateCreator<KnowledgeSlice, [], [], KnowledgeSlice> = (set, get) => ({
  knowledgeItems: [],
  knowledgeStatus: "idle",
  knowledgeFilter: null,
  knowledgeSearch: "",
  knowledgeActions: {
    fetchKnowledge: async (projectId: string, type?: KnowledgeType, search?: string) => {
      set({ knowledgeStatus: "loading" })
      try {
        const params = new URLSearchParams()
        if (type) params.set("type", type)
        if (search) params.set("search", search)
        const queryString = params.toString()
        const url = `/api/projects/${projectId}/knowledge/${queryString ? `?${queryString}` : ""}`
        const items = await api.get<KnowledgeItem[]>(url)
        set({ knowledgeItems: items, knowledgeStatus: "loaded" })
      } catch {
        set({ knowledgeStatus: "error" })
      }
    },

    createKnowledge: async (projectId: string, data: Partial<KnowledgeItem> & { type: KnowledgeType }) => {
      const item = await api.post<KnowledgeItem>(`/api/projects/${projectId}/knowledge/`, data)
      const items = get().knowledgeItems
      set({ knowledgeItems: [...items, item] })
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

    promoteKnowledge: async (projectId: string, id: string, data: { target_type: "rule" | "query"; name: string; [key: string]: unknown }) => {
      const item = await api.post<KnowledgeItem>(`/api/projects/${projectId}/knowledge/${id}/promote/`, data)
      const items = get().knowledgeItems.map((i) => (i.id === id ? item : i))
      set({ knowledgeItems: items })
      return item
    },

    setFilter: (type: KnowledgeType | null) => {
      set({ knowledgeFilter: type })
    },

    setSearch: (search: string) => {
      set({ knowledgeSearch: search })
    },
  },
})

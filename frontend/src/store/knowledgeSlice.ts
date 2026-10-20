import type { StateCreator } from "zustand"
import { api } from "@/api/client"
import type { DomainSlice } from "./domainSlice"
import type { AccountSessionScope } from "./accountSession"
import { createWorkspaceRequestGuard } from "./workspaceRequest"

// AgentLearning rows moved to the Memory page as workspace memory (#849).
export type KnowledgeType = "entry"

// A note is injected into every prompt; a reference document (#913) is only
// listed there, and the agent reads it on demand.
export type KnowledgeKind = "note" | "reference"

// Backend KnowledgeEntry model
export interface KnowledgeEntryItem {
  id: string
  type: "entry"
  kind: KnowledgeKind
  title: string
  description: string
  source_format: "" | "json" | "csv" | "tsv" | "md" | "txt"
  // In the list, a reference document's content is only a preview; see content_truncated.
  content: string
  content_truncated: boolean
  size: number
  tags: string[]
  created_at: string
  updated_at: string
}

export const REFERENCE_UPLOAD_EXTENSIONS = [".json", ".csv", ".tsv", ".md", ".txt"] as const
export const REFERENCE_UPLOAD_MAX_BYTES = 2 * 1024 * 1024

export interface ReferenceUploadFields {
  title?: string
  description?: string
}

export type KnowledgeItem = KnowledgeEntryItem

export function getKnowledgeItemName(item: KnowledgeItem): string {
  switch (item.type) {
    case "entry":
      return item.title
  }
}

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
    fetchKnowledge: (options?: { type?: KnowledgeType; search?: string; page?: number; pageSize?: number }) => Promise<void>
    createKnowledge: (data: Partial<KnowledgeItem> & { type: KnowledgeType }) => Promise<KnowledgeItem>
    updateKnowledge: (id: string, data: Partial<KnowledgeItem>) => Promise<KnowledgeItem>
    deleteKnowledge: (id: string) => Promise<void>
    exportKnowledge: () => Promise<void>
    importKnowledge: (file: File) => Promise<void>
    uploadReferenceDocument: (file: File, fields?: ReferenceUploadFields) => Promise<KnowledgeItem>
    fetchKnowledgeItem: (id: string) => Promise<KnowledgeItem>
    setFilter: (type: KnowledgeType | null) => void
    setSearch: (search: string) => void
  }
}

export const createKnowledgeSlice: StateCreator<KnowledgeSlice & DomainSlice & AccountSessionScope, [], [], KnowledgeSlice> = (set, get) => {
  const requests = createWorkspaceRequestGuard(get)
  return {
    knowledgeItems: [],
    knowledgeStatus: "idle",
    knowledgeError: null,
    knowledgePagination: null,
    knowledgeFilter: null,
    knowledgeSearch: "",
    knowledgeActions: {
      fetchKnowledge: async (options?) => {
        const isCurrent = requests.start("list")
        set({ knowledgeStatus: "loading", knowledgeError: null })
        try {
          const activeDomainId = get().activeDomainId
          if (!activeDomainId) throw new Error("No active domain selected.")
          const params = new URLSearchParams()
          if (options?.type) params.set("type", options.type)
          if (options?.search) params.set("search", options.search)
          if (options?.page) params.set("page", String(options.page))
          if (options?.pageSize) params.set("page_size", String(options.pageSize))
          const queryString = params.toString()
          const url = `/api/workspaces/${activeDomainId}/knowledge/${queryString ? `?${queryString}` : ""}`
          const response = await api.get<PaginatedKnowledgeResponse>(url)
          if (!isCurrent()) return
          set({
            knowledgeItems: response.results,
            knowledgePagination: response.pagination,
            knowledgeStatus: "loaded",
            knowledgeError: null,
          })
        } catch (error) {
          if (!isCurrent()) return
          set({
            knowledgeStatus: "error",
            knowledgePagination: null,
            knowledgeError: error instanceof Error ? error.message : "Failed to load knowledge items",
          })
        }
      },

      createKnowledge: async (data: Partial<KnowledgeItem> & { type: KnowledgeType }) => {
        const isCurrent = requests.start()
        const activeDomainId = get().activeDomainId
        if (!activeDomainId) throw new Error("No active domain selected.")
        const item = await api.post<KnowledgeItem>(`/api/workspaces/${activeDomainId}/knowledge/`, data)
        if (!isCurrent()) return item
        const items = get().knowledgeItems
        set({ knowledgeItems: [item, ...items] })
        return item
      },

      updateKnowledge: async (id: string, data: Partial<KnowledgeItem>) => {
        const isCurrent = requests.start()
        const activeDomainId = get().activeDomainId
        if (!activeDomainId) throw new Error("No active domain selected.")
        const item = await api.put<KnowledgeItem>(`/api/workspaces/${activeDomainId}/knowledge/${id}/`, data)
        if (!isCurrent()) return item
        const items = get().knowledgeItems.map((i) => (i.id === id ? item : i))
        set({ knowledgeItems: items })
        return item
      },

      deleteKnowledge: async (id: string) => {
        const isCurrent = requests.start()
        const activeDomainId = get().activeDomainId
        if (!activeDomainId) throw new Error("No active domain selected.")
        await api.delete<void>(`/api/workspaces/${activeDomainId}/knowledge/${id}/`)
        if (!isCurrent()) return
        const items = get().knowledgeItems.filter((i) => i.id !== id)
        set({ knowledgeItems: items })
      },

      exportKnowledge: async () => {
        const { activeDomainId, accountSession } = get()
        if (!accountSession.isCurrent()) return
        if (!activeDomainId) throw new Error("No active domain selected.")
        const blob = await api.getBlob(`/api/workspaces/${activeDomainId}/knowledge/export/`)
        // A fenced store write cannot stop an old response from downloading.
        if (!accountSession.isCurrent()) return
        const url = URL.createObjectURL(blob)
        const a = document.createElement("a")
        a.href = url
        a.download = `knowledge-export.zip`
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        URL.revokeObjectURL(url)
      },

      importKnowledge: async (file: File) => {
        const isCurrent = requests.start()
        const activeDomainId = get().activeDomainId
        if (!activeDomainId) throw new Error("No active domain selected.")
        const formData = new FormData()
        formData.append("file", file)
        await api.upload(`/api/workspaces/${activeDomainId}/knowledge/import/`, formData)
        if (!isCurrent()) return
        await get().knowledgeActions.fetchKnowledge()
      },

      uploadReferenceDocument: async (file: File, fields?: ReferenceUploadFields) => {
        const isCurrent = requests.start()
        const activeDomainId = get().activeDomainId
        if (!activeDomainId) throw new Error("No active domain selected.")
        const formData = new FormData()
        formData.append("file", file)
        if (fields?.title) formData.append("title", fields.title)
        if (fields?.description) formData.append("description", fields.description)
        const item = await api.upload<KnowledgeItem>(
          `/api/workspaces/${activeDomainId}/knowledge/upload/`,
          formData,
        )
        if (!isCurrent()) return item
        set({ knowledgeItems: [item, ...get().knowledgeItems] })
        return item
      },

      fetchKnowledgeItem: async (id: string) => {
        const activeDomainId = get().activeDomainId
        if (!activeDomainId) throw new Error("No active domain selected.")
        return api.get<KnowledgeItem>(`/api/workspaces/${activeDomainId}/knowledge/${id}/`)
      },

      setFilter: (type: KnowledgeType | null) => {
        set({ knowledgeFilter: type })
      },

      setSearch: (search: string) => {
        set({ knowledgeSearch: search })
      },
    },
  }
}

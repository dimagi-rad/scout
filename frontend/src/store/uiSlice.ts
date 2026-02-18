import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export interface Thread {
  id: string
  title: string
  created_at: string
  updated_at: string
  is_shared: boolean
  is_public: boolean
  share_token: string | null
}

export interface ThreadShareState {
  id: string
  is_shared: boolean
  is_public: boolean
  share_token: string | null
}

export type ThreadsStatus = "idle" | "loading" | "loaded"

export interface UiSlice {
  threadId: string
  activeArtifactId: string | null
  threads: Thread[]
  threadsStatus: ThreadsStatus
  uiActions: {
    newThread: () => void
    selectThread: (id: string) => void
    fetchThreads: (tenantId: string) => Promise<void>
    updateThreadSharing: (
      threadId: string,
      data: { is_shared?: boolean; is_public?: boolean },
    ) => Promise<ThreadShareState>
    openArtifact: (id: string) => void
    closeArtifact: () => void
  }
}

export const createUiSlice: StateCreator<UiSlice, [], [], UiSlice> = (set) => ({
  threadId: crypto.randomUUID(),
  activeArtifactId: null,
  threads: [],
  threadsStatus: "idle",
  uiActions: {
    newThread: () => {
      set({ threadId: crypto.randomUUID(), activeArtifactId: null })
    },
    selectThread: (id: string) => {
      set({ threadId: id, activeArtifactId: null })
    },
    fetchThreads: async (tenantId: string) => {
      set({ threadsStatus: "loading" })
      try {
        const threads = await api.get<Thread[]>(`/api/chat/threads/?tenant_id=${tenantId}`)
        set({ threads, threadsStatus: "loaded" })
      } catch {
        set({ threads: [], threadsStatus: "loaded" })
      }
    },
    updateThreadSharing: async (
      threadId: string,
      data: { is_shared?: boolean; is_public?: boolean },
    ) => {
      const result = await api.patch<ThreadShareState>(
        `/api/chat/threads/${threadId}/share/`,
        data,
      )
      set((state) => ({
        threads: state.threads.map((t) =>
          t.id === threadId
            ? { ...t, is_shared: result.is_shared, is_public: result.is_public, share_token: result.share_token }
            : t,
        ),
      }))
      return result
    },
    openArtifact: (id: string) => {
      set({ activeArtifactId: id })
    },
    closeArtifact: () => {
      set({ activeArtifactId: null })
    },
  },
})

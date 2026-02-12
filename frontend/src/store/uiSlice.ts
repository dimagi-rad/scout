import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export interface Thread {
  id: string
  title: string
  created_at: string
  updated_at: string
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
    fetchThreads: (projectId: string) => Promise<void>
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
    fetchThreads: async (projectId: string) => {
      set({ threadsStatus: "loading" })
      try {
        const threads = await api.get<Thread[]>(`/api/chat/threads/?project_id=${projectId}`)
        set({ threads, threadsStatus: "loaded" })
      } catch {
        set({ threads: [], threadsStatus: "loaded" })
      }
    },
    openArtifact: (id: string) => {
      set({ activeArtifactId: id })
    },
    closeArtifact: () => {
      set({ activeArtifactId: null })
    },
  },
})

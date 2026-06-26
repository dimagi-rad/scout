import type { StateCreator } from "zustand"
import { api } from "@/api/client"
import { markThreadViewed } from "@/api/threads"

export interface Thread {
  id: string
  title: string
  created_at: string
  updated_at: string
  is_shared: boolean
  is_public: boolean
  share_token: string | null
  last_viewed_at: string | null
}

export interface ThreadShareState {
  id: string
  is_shared: boolean
  is_public: boolean
  share_token: string | null
}

export type ThreadsStatus = "idle" | "loading" | "loaded" | "error"

export interface UiSlice {
  threadId: string
  activeArtifactId: string | null
  threads: Thread[]
  threadsStatus: ThreadsStatus
  uiActions: {
    newThread: () => void
    selectThread: (id: string) => Promise<void>
    fetchThreads: (workspaceId: string) => Promise<void>
    updateThreadSharing: (
      threadId: string,
      data: { is_shared?: boolean; is_public?: boolean },
      workspaceId: string,
    ) => Promise<ThreadShareState>
    openArtifact: (id: string) => void
    closeArtifact: () => void
  }
}

export const createUiSlice: StateCreator<UiSlice, [], [], UiSlice> = (set, get) => ({
  threadId: crypto.randomUUID(),
  activeArtifactId: null,
  threads: [],
  threadsStatus: "idle",
  uiActions: {
    newThread: () => {
      set({ threadId: crypto.randomUUID(), activeArtifactId: null })
    },
    selectThread: async (id: string) => {
      set({ threadId: id, activeArtifactId: null })
      const workspaceId = (get() as { activeDomainId?: string | null }).activeDomainId
      if (workspaceId) {
        try {
          await markThreadViewed(workspaceId, id)
        } catch {
          // Best-effort; failure does not block thread selection.
        }
        // Refresh the threads list so last_viewed_at flows through and the
        // green-dot indicator clears.
        await get().uiActions.fetchThreads(workspaceId)
      }
    },
    fetchThreads: async (workspaceId: string) => {
      set({ threadsStatus: "loading" })
      try {
        const threads = await api.get<Thread[]>(`/api/workspaces/${workspaceId}/threads/`)
        set({ threads, threadsStatus: "loaded" })
      } catch (error) {
        // Distinguish an outage from a genuinely empty history (07#7): a failed
        // load must NOT report "loaded" with an empty array — that rendered as
        // "no conversations" during a DB/checkpointer blip, as if every
        // conversation had been deleted. Keep any threads already shown and flag
        // the error so the sidebar can surface a retry affordance instead.
        console.error("[Scout] Failed to load threads:", error)
        set({ threadsStatus: "error" })
      }
    },
    updateThreadSharing: async (
      threadId: string,
      data: { is_shared?: boolean; is_public?: boolean },
      workspaceId: string,
    ) => {
      const result = await api.patch<ThreadShareState>(
        `/api/workspaces/${workspaceId}/threads/${threadId}/share/`,
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

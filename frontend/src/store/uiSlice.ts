import type { StateCreator } from "zustand"
import { ApiError, api } from "@/api/client"
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
  // Actionable message when the user lost upstream (tenant) access to the
  // workspace — distinct from a retryable outage. null in every other case.
  threadsAccessLostMessage: string | null
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
  threadsAccessLostMessage: null,
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
        // Refresh so last_viewed_at flows through and the green-dot clears.
        await get().uiActions.fetchThreads(workspaceId)
      }
    },
    fetchThreads: async (workspaceId: string) => {
      set({ threadsStatus: "loading" })
      try {
        const threads = await api.get<Thread[]>(`/api/workspaces/${workspaceId}/threads/`)
        set({ threads, threadsStatus: "loaded", threadsAccessLostMessage: null })
      } catch (error) {
        // Distinguish an outage from genuinely-empty history (07#7): reporting
        // "loaded" with [] reads as "all conversations deleted" during a
        // DB/checkpointer blip. Keep shown threads and flag the error for retry.
        console.error("[Scout] Failed to load threads:", error)
        // Lost upstream tenant access is not retryable: show the server's
        // actionable message instead of the generic "couldn't load" + retry.
        const accessLost =
          error instanceof ApiError &&
          typeof error.body === "object" &&
          error.body !== null &&
          (error.body as { reason?: string }).reason === "tenant_access_lost"
        set({
          threadsStatus: "error",
          threadsAccessLostMessage: accessLost ? error.message : null,
        })
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

import type { StateCreator } from "zustand"
import { ApiError, api } from "@/api/client"
import { markThreadViewed } from "@/api/threads"
import { workspaceApi } from "@/api/workspaces"
import type { DomainSlice } from "./domainSlice"
import { createWorkspaceRequestGuard } from "./workspaceRequest"

export interface Thread {
  id: string
  title: string
  history_title?: string
  title_is_custom: boolean
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

const ACCESS_DENIAL_REASONS = new Set([
  "tenant_access_lost",
  "credential_missing",
  "credential_expired",
  "upstream_access_lost",
  "verification_unavailable",
  "verification_in_progress",
])

// A lost-access denial is also rechecked on request: once an admin restores access
// upstream, only an explicit verification can restore the archived membership.
const RECHECKABLE_REASONS = new Set([
  "tenant_access_lost",
  "upstream_access_lost",
  "verification_unavailable",
  "verification_in_progress",
])

function accessDenial(error: unknown): { message: string; retryable: boolean } | null {
  if (!(error instanceof ApiError) || typeof error.body !== "object" || error.body === null) {
    return null
  }
  const body = error.body as { reason?: unknown }
  if (typeof body.reason !== "string" || !ACCESS_DENIAL_REASONS.has(body.reason)) return null
  return { message: error.message, retryable: RECHECKABLE_REASONS.has(body.reason) }
}

export interface UiSlice {
  threadId: string
  activeArtifactId: string | null
  threads: Thread[]
  threadsStatus: ThreadsStatus
  // Actionable message when the user lost upstream (tenant) access to the
  // workspace — distinct from a retryable outage. null in every other case.
  threadsAccessLostMessage: string | null
  // An explicit upstream recheck can resolve this denial (a temporary failure, or
  // access an admin may since have restored), so offer "Retry verification".
  threadsAccessRetryable: boolean
  uiActions: {
    newThread: () => void
    selectThread: (id: string) => Promise<void>
    fetchThreads: (workspaceId: string) => Promise<void>
    retryAccessVerification: (workspaceId: string) => Promise<void>
    updateThreadTitle: (
      threadId: string,
      title: string,
      workspaceId: string,
    ) => Promise<Thread>
    updateThreadSharing: (
      threadId: string,
      data: { is_shared?: boolean; is_public?: boolean },
      workspaceId: string,
    ) => Promise<ThreadShareState>
    openArtifact: (id: string) => void
    closeArtifact: () => void
  }
}

export const createUiSlice: StateCreator<UiSlice & DomainSlice, [], [], UiSlice> = (set, get) => {
  const requests = createWorkspaceRequestGuard(get)
  return {
    threadId: crypto.randomUUID(),
    activeArtifactId: null,
    threads: [],
    threadsStatus: "idle",
    threadsAccessLostMessage: null,
    threadsAccessRetryable: false,
    uiActions: {
      newThread: () => {
        set({ threadId: crypto.randomUUID(), activeArtifactId: null })
      },
      selectThread: async (id: string) => {
        const isCurrent = requests.start()
        set({ threadId: id, activeArtifactId: null })
        const workspaceId = get().activeDomainId
        if (workspaceId) {
          try {
            await markThreadViewed(workspaceId, id)
          } catch {
            // Best-effort; failure does not block thread selection.
          }
          if (!isCurrent()) return
          // Refresh so last_viewed_at flows through and the green-dot clears.
          await get().uiActions.fetchThreads(workspaceId)
        }
      },
      fetchThreads: async (workspaceId: string) => {
        if (workspaceId !== get().activeDomainId) return
        const isCurrent = requests.start("threads", workspaceId)
        set({ threadsStatus: "loading" })
        try {
          const threads = await api.get<Thread[]>(`/api/workspaces/${workspaceId}/threads/`)
          if (!isCurrent()) return
          set({
            threads,
            threadsStatus: "loaded",
            threadsAccessLostMessage: null,
            threadsAccessRetryable: false,
          })
        } catch (error) {
          if (!isCurrent()) return
          // Distinguish an outage from genuinely-empty history (07#7): reporting
          // "loaded" with [] reads as "all conversations deleted" during a
          // DB/checkpointer blip. Keep shown threads and flag the error for retry.
          console.error("[Scout] Failed to load threads:", error)
          // An access denial carries an actionable server message; show it
          // instead of the generic "couldn't load" + retry.
          const denial = accessDenial(error)
          set({
            threadsStatus: "error",
            threadsAccessLostMessage: denial?.message ?? null,
            threadsAccessRetryable: denial?.retryable === true,
          })
        }
      },
      retryAccessVerification: async (workspaceId: string) => {
        const isCurrent = requests.start("threads", workspaceId)
        try {
          await workspaceApi.retryAccessVerification(workspaceId)
        } catch (error) {
          if (!isCurrent()) return
          // The retry already reported the authoritative outcome; refetching would
          // only run a second recheck against the same failing provider.
          const denial = accessDenial(error)
          if (denial) {
            set({
              threadsStatus: "error",
              threadsAccessLostMessage: denial.message,
              threadsAccessRetryable: denial.retryable,
            })
            return
          }
          console.error("[Scout] Access verification retry failed:", error)
          return
        }
        if (!isCurrent()) return
        // The workspace list's has_access gates the lost-access modal; refresh it too.
        await Promise.all([
          get().domainActions.fetchDomains(),
          get().uiActions.fetchThreads(workspaceId),
        ])
      },
      updateThreadTitle: async (threadId: string, title: string, workspaceId: string) => {
        const isCurrent = requests.start(undefined, workspaceId)
        const result = await api.patch<Thread>(
          `/api/workspaces/${workspaceId}/threads/${threadId}/`,
          { title },
        )
        if (!isCurrent()) return result
        set((state) => {
          const exists = state.threads.some((t) => t.id === threadId)
          return {
            threads: exists
              ? state.threads.map((t) => (t.id === threadId ? { ...t, ...result } : t))
              : [result, ...state.threads],
          }
        })
        return result
      },
      updateThreadSharing: async (
        threadId: string,
        data: { is_shared?: boolean; is_public?: boolean },
        workspaceId: string,
      ) => {
        const isCurrent = requests.start(undefined, workspaceId)
        const result = await api.patch<ThreadShareState>(
          `/api/workspaces/${workspaceId}/threads/${threadId}/share/`,
          data,
        )
        if (!isCurrent()) return result
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
  }
}

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
  last_viewed_at: string | null
}

export type ThreadsStatus = "idle" | "loading" | "loaded" | "error"

const ACCESS_DENIAL_REASONS = [
  "tenant_access_lost",
  "credential_missing",
  "credential_expired",
  "upstream_access_lost",
  "verification_unavailable",
  "verification_in_progress",
  // A workspace with no sources (#381): only a delete resolves it, so never recheckable.
  "no_sources",
] as const

export type AccessDenialReason = (typeof ACCESS_DENIAL_REASONS)[number]

// A lost-access denial is also rechecked on request: once an admin restores access
// upstream, only an explicit verification can restore the archived membership.
const RECHECKABLE_REASONS: ReadonlySet<AccessDenialReason> = new Set([
  "tenant_access_lost",
  "upstream_access_lost",
  "verification_unavailable",
  "verification_in_progress",
])

// The threads fetch is what notices these upstream and archives the membership; the
// workspace list's has_access, which gates the lost-access modal, lags until refetched.
export const ACCESS_LOSS_REASONS: ReadonlySet<AccessDenialReason> = new Set([
  "tenant_access_lost",
  "upstream_access_lost",
  "credential_missing",
  "credential_expired",
])

interface AccessDenial {
  reason: AccessDenialReason
  message: string
  retryable: boolean
}

function isAccessDenialReason(value: unknown): value is AccessDenialReason {
  return (ACCESS_DENIAL_REASONS as readonly unknown[]).includes(value)
}

function accessDenial(error: unknown): AccessDenial | null {
  if (!(error instanceof ApiError) || typeof error.body !== "object" || error.body === null) {
    return null
  }
  const { reason } = error.body as { reason?: unknown }
  if (!isAccessDenialReason(reason)) return null
  return { reason, message: error.message, retryable: RECHECKABLE_REASONS.has(reason) }
}

export interface UiSlice {
  threadId: string
  activeArtifactId: string | null
  threads: Thread[]
  threadsStatus: ThreadsStatus
  // Why the threads fetch was refused access — distinct from a retryable outage.
  // null in every other case.
  threadsAccessDenialReason: AccessDenialReason | null
  // An explicit upstream recheck can resolve this denial (a temporary failure, or
  // access an admin may since have restored), so offer "Retry verification".
  threadsAccessRetryable: boolean
  // The server's message when "Retry verification" was itself denied. Kept apart from
  // the threads denial so the lost-access gate shows only what the retry found.
  accessRetryOutcome: string | null
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
    threadsAccessDenialReason: null,
    threadsAccessRetryable: false,
    accessRetryOutcome: null,
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
            threadsAccessDenialReason: null,
            threadsAccessRetryable: false,
            accessRetryOutcome: null,
          })
        } catch (error) {
          if (!isCurrent()) return
          // Distinguish an outage from genuinely-empty history (07#7): reporting
          // "loaded" with [] reads as "all conversations deleted" during a
          // DB/checkpointer blip. Keep shown threads and flag the error for retry.
          console.error("[Scout] Failed to load threads:", error)
          // An access denial gets reason-specific guidance instead of the generic
          // "couldn't load" + retry.
          const denial = accessDenial(error)
          set({
            threadsStatus: "error",
            threadsAccessDenialReason: denial?.reason ?? null,
            threadsAccessRetryable: denial?.retryable === true,
            accessRetryOutcome: null,
          })
          // Can't loop: threads refetch on a workspace switch, not when the list changes.
          if (denial && ACCESS_LOSS_REASONS.has(denial.reason)) {
            void get().domainActions.revalidateDomains({ fresh: true })
          }
        }
      },
      retryAccessVerification: async (workspaceId: string) => {
        const isCurrent = requests.start("threads", workspaceId)
        set({ accessRetryOutcome: null })
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
              threadsAccessDenialReason: denial.reason,
              threadsAccessRetryable: denial.retryable,
              accessRetryOutcome: denial.message,
            })
            // The gate lists missing sources from the workspace list, not this response.
            if (ACCESS_LOSS_REASONS.has(denial.reason)) {
              void get().domainActions.revalidateDomains({ fresh: true })
            }
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
      openArtifact: (id: string) => {
        set({ activeArtifactId: id })
      },
      closeArtifact: () => {
        set({ activeArtifactId: null })
      },
    },
  }
}

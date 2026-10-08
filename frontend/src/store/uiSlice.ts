import type { StateCreator } from "zustand"
import { ApiError, api } from "@/api/client"
import { markThreadViewed } from "@/api/threads"
import { workspaceApi } from "@/api/workspaces"
import type { DomainSlice } from "./domainSlice"
import { createWorkspaceRequestGuard } from "./workspaceRequest"
import {
  ACCESS_DENIAL_REASONS,
  RECHECKABLE_REASONS,
  type AccessDenialReason,
} from "@/lib/accessReasons"
import { shortThreadTitle } from "@/lib/threadTitle"
import { forgetLocalThread, newLocalThreadId } from "./localThreads"

export type { AccessDenialReason }

export type ThreadTitleSource = "first_message" | "generated" | "user"

export interface Thread {
  id: string
  // The one title the header and sidebar both show: the first message until a
  // short title is generated, or the user's rename.
  title: string
  title_is_custom: boolean
  title_source: ThreadTitleSource
  created_at: string
  updated_at: string
  last_viewed_at: string | null
  /** An agent run holds the thread's turn (in any tab, or a background resume). */
  turn_running?: boolean
}

export type ThreadsStatus = "idle" | "loading" | "loaded" | "error"

// Denials that mean coverage was archived, which flips the workspace list's has_access
// (the lost-access gate) only once refetched. credential_missing is a freshness denial
// with coverage still granted, so a refetch could never open the gate.
export const ACCESS_LOSS_REASONS: ReadonlySet<AccessDenialReason> = new Set([
  "tenant_access_lost",
  "upstream_access_lost",
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
  // Threads whose turn this tab is streaming; it knows when they end, so the sidebar
  // shows them running without polling the list for them (#856).
  localTurnThreadIds: ReadonlySet<string>
  uiActions: {
    newThread: () => void
    selectThread: (id: string) => Promise<void>
    fetchThreads: (workspaceId: string) => Promise<void>
    /** Lists a new chat whose first message is on its way, until the server lists it. */
    addSendingThread: (
      workspaceId: string,
      threadId: string,
      text: string,
      isNewChat: boolean,
    ) => void
    /** A send got its response, so the server's list now decides whether the thread is listed. */
    settleSendingThread: (workspaceId: string, threadId: string) => void
    retryAccessVerification: (workspaceId: string) => Promise<void>
    updateThreadTitle: (
      threadId: string,
      title: string,
      workspaceId: string,
    ) => Promise<Thread>
    openArtifact: (id: string) => void
    closeArtifact: () => void
    startLocalTurn: (threadId: string) => void
    /** The turn this tab streamed is over: its row stops showing running until a refetch says otherwise. */
    endLocalTurn: (threadId: string) => void
    /** The chat that streamed them is gone; the server's flag decides from here. */
    forgetLocalTurns: () => void
  }
}

export const createUiSlice: StateCreator<UiSlice & DomainSlice, [], [], UiSlice> = (set, get) => {
  const requests = createWorkspaceRequestGuard(get)
  // Per workspace. A first turn's response arrives only once its agent is built, which
  // can take seconds; until then a refetch would drop the new chat from the sidebar (#859).
  // fetchThreads is the one place that merges these back in.
  const sendingThreads = new Map<string, Map<string, Thread>>()
  // Answered first sends whose row the next applied list confirms; by thread id.
  const settledThreads = new Map<string, string>()
  // Chats whose first send was refused before the server made the row, so a resend is
  // still their first and is listed at once too. Only ever adds a placeholder: a wrong
  // entry costs a brief row, unlike marking the chat local, which skips its history.
  const refusedFirstSends = new Set<string>()
  const withSending = (workspaceId: string, threads: Thread[]): Thread[] => {
    const listed = new Set(threads.map((thread) => thread.id))
    for (const [threadId, sentIn] of settledThreads) {
      if (sentIn !== workspaceId) continue
      settledThreads.delete(threadId)
      if (!listed.has(threadId)) refusedFirstSends.add(threadId)
    }
    const sending = [...(sendingThreads.get(workspaceId)?.values() ?? [])]
      .filter((thread) => !listed.has(thread.id))
      .reverse()
    return sending.length ? [...sending, ...threads] : threads
  }
  return {
    threadId: newLocalThreadId(),
    activeArtifactId: null,
    threads: [],
    threadsStatus: "idle",
    threadsAccessDenialReason: null,
    threadsAccessRetryable: false,
    accessRetryOutcome: null,
    localTurnThreadIds: new Set(),
    uiActions: {
      startLocalTurn: (threadId: string) => {
        set((state) => ({ localTurnThreadIds: new Set([...state.localTurnThreadIds, threadId]) }))
      },
      endLocalTurn: (threadId: string) => {
        set((state) => {
          const local = new Set(state.localTurnThreadIds)
          local.delete(threadId)
          return {
            localTurnThreadIds: local,
            threads: state.threads.map((thread) =>
              thread.id === threadId && thread.turn_running
                ? { ...thread, turn_running: false }
                : thread,
            ),
          }
        })
      },
      forgetLocalTurns: () => {
        set({ localTurnThreadIds: new Set() })
      },
      newThread: () => {
        set({ threadId: newLocalThreadId(), activeArtifactId: null })
      },
      selectThread: async (id: string) => {
        // Opened from the list: another tab may have sent in it, so it has history.
        forgetLocalThread(id)
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
            threads: withSending(workspaceId, threads),
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
      addSendingThread: (
        workspaceId: string,
        threadId: string,
        text: string,
        isNewChat: boolean,
      ) => {
        const firstSend = refusedFirstSends.delete(threadId) || isNewChat
        if (!firstSend || workspaceId !== get().activeDomainId) return
        if (get().threads.some((thread) => thread.id === threadId)) return
        const now = new Date().toISOString()
        const placeholder: Thread = {
          id: threadId,
          title: shortThreadTitle(text),
          title_is_custom: false,
          title_source: "first_message",
          created_at: now,
          updated_at: now,
          last_viewed_at: now,
        }
        let sending = sendingThreads.get(workspaceId)
        if (!sending) {
          sending = new Map()
          sendingThreads.set(workspaceId, sending)
        }
        sending.set(threadId, placeholder)
        set((state) => ({ threads: [placeholder, ...state.threads] }))
      },
      settleSendingThread: (workspaceId: string, threadId: string) => {
        const wasSending = sendingThreads.get(workspaceId)?.delete(threadId) === true
        // A retried send (after a busy 503, say) has no placeholder but may still be unlisted.
        const unlisted = !get().threads.some((thread) => thread.id === threadId)
        if (wasSending) settledThreads.set(threadId, workspaceId)
        if (wasSending || unlisted) void get().uiActions.fetchThreads(workspaceId)
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

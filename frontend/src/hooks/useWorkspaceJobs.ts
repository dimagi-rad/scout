import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  jobsApi,
  type ActiveJob,
  type PendingRequest,
  type RecentTermination,
  type WorkspaceLoad,
} from "@/api/jobs"

const POLL_INTERVAL_MS = 3000

interface State {
  workspaceId: string | null
  jobs: ActiveJob[]
  workspaceLoads: WorkspaceLoad[]
  recentTerminations: RecentTermination[]
  pendingRequests: Record<string, PendingRequest>
  lastError: string | null
}

/** A local change to a held request the poll has not caught up with yet. */
interface PendingOverride {
  value: PendingRequest | null
  seq: number
  /** How the override ends; see ``PendingOverrideMode``. */
  mode: PendingOverrideMode
  /** For "hide": the request hidden while the server still reports it. */
  hides?: string
}

/**
 * - "committed": the server already has the change, so a poll that started after
 *   it shows it and the override ends there.
 * - "inFlight": the request for it has not answered yet; only a later call ends it.
 * - "hide": the request is being sent by this tab and is hidden until the server
 *   stops reporting it (it stays, claimed, until that turn settles).
 */
export type PendingOverrideMode = "committed" | "inFlight" | "hide"

function overrideHolds(
  override: PendingOverride,
  polled: PendingRequest | undefined,
  seqAtStart: number,
): boolean {
  if (override.mode === "inFlight") return true
  if (override.mode === "hide") return polled !== undefined && polled.request_id === override.hides
  return override.seq > seqAtStart
}

export interface UseWorkspaceJobs {
  jobs: ActiveJob[]
  /** Every in-flight load of the workspace, including ones other members or a
   *  refresh started, which have no job in the caller's threads. */
  workspaceLoads: WorkspaceLoad[]
  jobsByThreadId: Record<string, ActiveJob>
  /** Thread IDs whose job just transitioned to a terminal state on the most
   *  recent poll (gone from the active list), or whose held request just went
   *  out. Consumers should refetch thread messages for these IDs. Resets to []
   *  on the next poll cycle. */
  recentlyCompletedThreadIds: string[]
  /** ThreadJobs that terminated within the server's recent-termination
   *  window (default 30 minutes). Used to render an inline failure card on
   *  the run_materialization tool-call once the spinner clears. */
  recentTerminations: RecentTermination[]
  /** Lookup by tool_call_id so ChatMessage can find the termination for a
   *  specific run_materialization card in O(1). Failures with no
   *  tool_call_id (e.g. retry jobs not bound to a card) are excluded. */
  recentTerminationsByToolCallId: Record<string, RecentTermination>
  /** Requests held while a chat's data loads, by thread id: the last poll,
   *  overlaid with local changes it has not seen yet. */
  pendingByThreadId: Record<string, PendingRequest>
  /** Show a local change to a thread's held request (null: none) over the poll;
   *  ``mode`` says when the poll takes over again (default "committed"). */
  setPendingRequest: (
    threadId: string,
    pending: PendingRequest | null,
    mode?: Exclude<PendingOverrideMode, "hide">,
  ) => void
  /** Hide a request this tab is sending until the server stops reporting it. */
  hidePendingRequest: (threadId: string, requestId: string) => void
  /** Drop a local change, so the thread shows the server's copy again. */
  forgetPendingRequest: (threadId: string) => void
  refresh: () => Promise<void>
  /** Force an immediate poll without waiting for the next tick (called when
   *  the user just fired a chat action that may have started a job). */
  notifyJobLikelyStarted: () => void
}

/**
 * Polling implementation hook. Do NOT call this directly from components —
 * use `useWorkspaceJobs` from `@/contexts/WorkspaceJobsContext` instead so the
 * polling loop has a single owner. The provider is the only legitimate caller.
 */
export function useWorkspaceJobsImpl(workspaceId: string | null): UseWorkspaceJobs {
  const [state, setState] = useState<State>({
    jobs: [],
    workspaceId,
    workspaceLoads: [],
    recentTerminations: [],
    pendingRequests: {},
    lastError: null,
  })
  const [recentlyCompletedThreadIds, setRecentlyCompletedThreadIds] = useState<string[]>([])
  const [overrides, setOverrides] = useState<Record<string, PendingOverride>>({})
  const overrideSeqRef = useRef(0)
  // Polls overlap (the interval plus refreshes); an older one landing last must
  // not overwrite a newer snapshot or the diff the next poll is taken against.
  const pollSeqRef = useRef(0)
  const appliedPollRef = useRef(0)
  const prevThreadIdsRef = useRef<Set<string>>(new Set())
  // Thread id -> request id: a thread can send one request and hold the next
  // between two polls, which must still count as a send.
  const prevPendingRef = useRef<Map<string, string>>(new Map())
  const workspaceIdRef = useRef(workspaceId)
  useEffect(() => {
    workspaceIdRef.current = workspaceId
  }, [workspaceId])

  const fetchOnce = useCallback(async () => {
    if (!workspaceId) return
    // Local changes made after this poll left are newer than what it returns.
    const seqAtStart = overrideSeqRef.current
    const poll = ++pollSeqRef.current
    try {
      const data = await jobsApi.active(workspaceId)
      // A response for a workspace we have since left must not seed the new one's diff.
      if (workspaceIdRef.current !== workspaceId) return
      if (poll < appliedPollRef.current) return
      appliedPollRef.current = poll
      const currentThreadIds = new Set(data.jobs.map((j) => j.thread_id))
      const pendingRequests = data.pending_requests ?? {}
      const currentPending = new Map(
        Object.entries(pendingRequests).map(([threadId, pending]) => [threadId, pending.request_id]),
      )
      const justCompleted = new Set<string>()
      for (const prev of prevThreadIdsRef.current) {
        if (!currentThreadIds.has(prev)) justCompleted.add(prev)
      }
      // A held request leaves once its message is in the conversation.
      for (const [threadId, requestId] of prevPendingRef.current) {
        if (currentPending.get(threadId) !== requestId) justCompleted.add(threadId)
      }
      prevThreadIdsRef.current = currentThreadIds
      prevPendingRef.current = currentPending
      setState({
        jobs: data.jobs,
        workspaceId,
        workspaceLoads: data.workspace_loads ?? [],
        recentTerminations: data.recent_terminations ?? [],
        pendingRequests,
        lastError: null,
      })
      setOverrides((prev) => {
        const kept = Object.entries(prev).filter(([threadId, override]) =>
          overrideHolds(override, pendingRequests[threadId], seqAtStart),
        )
        return kept.length === Object.keys(prev).length ? prev : Object.fromEntries(kept)
      })
      if (justCompleted.size > 0) {
        setRecentlyCompletedThreadIds([...justCompleted])
      } else {
        // Functional updater so React skips the re-render when already empty;
        // otherwise every clean poll churns the ChatPanel reload effect.
        setRecentlyCompletedThreadIds((prev) => (prev.length === 0 ? prev : []))
      }
    } catch (e) {
      setState((s) => ({ ...s, lastError: String(e) }))
    }
  }, [workspaceId])

  useEffect(() => {
    if (!workspaceId) return
    // Reset cross-workspace state, else the new workspace's first poll falsely
    // reports the previous workspace's thread ids as "just completed".
    prevThreadIdsRef.current = new Set()
    prevPendingRef.current = new Map()
    setRecentlyCompletedThreadIds((prev) => (prev.length === 0 ? prev : []))
    setOverrides((prev) => (Object.keys(prev).length === 0 ? prev : {}))
    let cancelled = false
    let interval: ReturnType<typeof setInterval> | null = null

    // Gate polling on tab visibility (arch #254, 05#6): a hidden tab needs no
    // live job status, and each poll triggers an API-side janitor reconciliation.
    // Pausing while hidden removes that idle load; resume catches up immediately.
    const startPolling = () => {
      if (interval !== null) return
      interval = setInterval(() => {
        if (!cancelled) void fetchOnce()
      }, POLL_INTERVAL_MS)
    }
    const stopPolling = () => {
      if (interval !== null) {
        clearInterval(interval)
        interval = null
      }
    }
    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        if (!cancelled) void fetchOnce()
        startPolling()
      } else {
        stopPolling()
      }
    }

    document.addEventListener("visibilitychange", handleVisibility)
    // Fire immediately on mount so the UI populates without waiting one tick.
    void fetchOnce()
    if (document.visibilityState === "visible") startPolling()

    return () => {
      cancelled = true
      stopPolling()
      document.removeEventListener("visibilitychange", handleVisibility)
    }
  }, [workspaceId, fetchOnce])

  const setPendingRequest = useCallback(
    (
      threadId: string,
      pending: PendingRequest | null,
      mode: Exclude<PendingOverrideMode, "hide"> = "committed",
    ) => {
      overrideSeqRef.current += 1
      const seq = overrideSeqRef.current
      setOverrides((prev) => ({ ...prev, [threadId]: { value: pending, seq, mode } }))
    },
    [],
  )

  const hidePendingRequest = useCallback((threadId: string, requestId: string) => {
    overrideSeqRef.current += 1
    const seq = overrideSeqRef.current
    setOverrides((prev) => ({
      ...prev,
      [threadId]: { value: null, seq, mode: "hide", hides: requestId },
    }))
  }, [])

  const forgetPendingRequest = useCallback((threadId: string) => {
    setOverrides((prev) => {
      if (!(threadId in prev)) return prev
      const next = { ...prev }
      delete next[threadId]
      return next
    })
  }, [])

  const sameWorkspace = state.workspaceId === workspaceId
  const pendingByThreadId = useMemo(() => {
    const merged: Record<string, PendingRequest> = sameWorkspace
      ? { ...state.pendingRequests }
      : {}
    for (const [threadId, override] of Object.entries(overrides)) {
      if (override.value === null) delete merged[threadId]
      else merged[threadId] = override.value
    }
    return merged
  }, [sameWorkspace, state.pendingRequests, overrides])

  const jobsByThreadId = state.jobs.reduce<Record<string, ActiveJob>>((acc, j) => {
    acc[j.thread_id] = j
    return acc
  }, {})

  const recentTerminationsByToolCallId = state.recentTerminations.reduce<
    Record<string, RecentTermination>
  >((acc, t) => {
    if (t.tool_call_id && !acc[t.tool_call_id]) acc[t.tool_call_id] = t
    return acc
  }, {})

  return {
    jobs: state.jobs,
    // Else a switch shows the previous workspace's load until the first poll lands.
    workspaceLoads: sameWorkspace ? state.workspaceLoads : [],
    jobsByThreadId,
    recentlyCompletedThreadIds,
    recentTerminations: state.recentTerminations,
    recentTerminationsByToolCallId,
    pendingByThreadId,
    setPendingRequest,
    hidePendingRequest,
    forgetPendingRequest,
    refresh: fetchOnce,
    notifyJobLikelyStarted: fetchOnce,
  }
}

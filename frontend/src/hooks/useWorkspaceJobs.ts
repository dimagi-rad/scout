import { useCallback, useEffect, useState } from "react"
import { jobsApi, type ActiveJob } from "@/api/jobs"

const POLL_INTERVAL_MS = 3000

interface State {
  jobs: ActiveJob[]
  lastError: string | null
}

export interface UseWorkspaceJobs {
  jobs: ActiveJob[]
  jobsByThreadId: Record<string, ActiveJob>
  refresh: () => Promise<void>
  /** Force an immediate poll without waiting for the next tick (called when
   *  the user just fired a chat action that may have started a job). */
  notifyJobLikelyStarted: () => void
}

export function useWorkspaceJobs(workspaceId: string | null): UseWorkspaceJobs {
  const [state, setState] = useState<State>({ jobs: [], lastError: null })

  const fetchOnce = useCallback(async () => {
    if (!workspaceId) return
    try {
      const data = await jobsApi.active(workspaceId)
      setState({ jobs: data.jobs, lastError: null })
    } catch (e) {
      setState((s) => ({ ...s, lastError: String(e) }))
    }
  }, [workspaceId])

  useEffect(() => {
    if (!workspaceId) return
    let cancelled = false
    const interval = setInterval(() => {
      if (!cancelled) void fetchOnce()
    }, POLL_INTERVAL_MS)
    // Fire immediately on mount so the UI populates without waiting one tick.
    void fetchOnce()
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [workspaceId, fetchOnce])

  const jobsByThreadId = state.jobs.reduce<Record<string, ActiveJob>>((acc, j) => {
    acc[j.thread_id] = j
    return acc
  }, {})

  return {
    jobs: state.jobs,
    jobsByThreadId,
    refresh: fetchOnce,
    notifyJobLikelyStarted: fetchOnce,
  }
}

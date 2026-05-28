import { api } from "@/api/client"

export type JobState = "pending" | "running" | "completed" | "failed" | "cancelled"

export type TerminationState = "completed" | "failed" | "cancelled"

export interface JobProgress {
  percent: number | null
  rows_loaded: number
  rows_total: number | null
  message: string | null
  source: string | null
  step: number | null
  total_steps: number | null
}

export interface ActiveJob {
  thread_job_id: string
  thread_id: string
  /** AI-SDK toolCallId of the run_materialization tool call this job is
   *  attached to. Used to scope progress + Stop UI to the specific tool-call
   *  card rather than every historical run_materialization message in the
   *  thread. */
  tool_call_id: string
  job_type: "materialization"
  state: JobState
  progress: JobProgress | null
  created_at: string
}

export interface RecentTermination {
  thread_job_id: string
  thread_id: string
  /** Empty string for retry jobs not bound to a specific tool-call card. */
  tool_call_id: string
  state: TerminationState
  completed_at: string | null
  error_summary: string
  retry_available: boolean
}

export interface ActiveJobsResponse {
  jobs: ActiveJob[]
  recent_terminations: RecentTermination[]
}

export const jobsApi = {
  active: (workspaceId: string) =>
    api.get<ActiveJobsResponse>(`/api/workspaces/${workspaceId}/jobs/active/`),
  cancel: (workspaceId: string, threadJobId: string) =>
    api.post<void>(`/api/workspaces/${workspaceId}/jobs/${threadJobId}/cancel/`, {}),
  retryMaterialization: (
    workspaceId: string,
    body: { thread_id?: string; tool_call_id?: string },
  ) =>
    api.post<{ status: string; thread_job_id?: string }>(
      `/api/workspaces/${workspaceId}/materialize/retry/`,
      body,
    ),
}

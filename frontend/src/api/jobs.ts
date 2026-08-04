import { api } from "@/api/client"

export type JobState = "pending" | "running" | "completed" | "failed" | "cancelled"

export type TerminationState = "completed" | "failed" | "cancelled"

export interface JobProgress {
  percent: number | null
  rows_loaded: number
  rows_total: number | null
  /** Display unit for the counts — "rows" for most sources; OCS messages
   *  report per-session progress as "sessions". */
  unit?: string
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

/** Stable server-side error codes. Branch on these, never on error_summary
 *  prose — see the reporting rules in apps/common/errors.py. */
export const AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
export const AUTH_ACCESS_DENIED = "AUTH_ACCESS_DENIED"

export interface CredentialFailure {
  /** Pipeline source name, e.g. "visits". */
  source: string
  /** An ErrorCode — AUTH_TOKEN_EXPIRED or AUTH_ACCESS_DENIED. */
  code: string
  /** "ocs" | "commcare" | "commcare_connect", or "" if unrecorded. */
  provider: string
}

export interface RecentTermination {
  thread_job_id: string
  thread_id: string
  /** Empty string for retry jobs not bound to a specific tool-call card. */
  tool_call_id: string
  state: TerminationState
  completed_at: string | null
  error_summary: string
  /** Machine-readable counterpart to error_summary. Only AUTH_TOKEN_EXPIRED
   *  warrants a Reconnect CTA: for AUTH_ACCESS_DENIED the credential is valid,
   *  so reconnecting mints the same token and loops the user (#372). */
  credential_failures: CredentialFailure[]
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

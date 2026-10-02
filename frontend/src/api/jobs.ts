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
  /** Position of the source being loaded within its load ("Source 2 of 4");
   *  null when the run is not part of a tracked load. */
  source_index: number | null
  source_total: number | null
  tenant_name: string | null
  created_at: string
}

/** One in-flight load of the workspace, whoever started it. */
export interface WorkspaceLoad {
  tenant_id: string
  tenant_name: string
  source_index: number
  source_total: number
  state: "started" | "discovering" | "loading" | "transforming"
  started_at: string
  progress: JobProgress | null
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

export interface PendingRequestPart {
  id: string
  text: string
  added_at: string
}

/** What the user typed while their chat's first data load ran, held server-side
 *  as one unsent message until the data can answer it. */
export interface PendingRequest {
  thread_id: string
  /** Names this request; a thread's next one, after this is sent, gets a new id. */
  request_id: string
  /** Bumped on every change; a change sent against an older version is refused. */
  version: number
  parts: PendingRequestPart[]
  /** "claimed" while a run is sending it. */
  state: "waiting" | "claimed"
  thread_job_id: string | null
  /** The state of the load that will send it, or null when it has none. */
  thread_job_state: JobState | null
  /** With no load of its own: whether a workspace load is under way, whose end sends it. */
  workspace_load_pending?: boolean
}

export interface ActiveJobsResponse {
  jobs: ActiveJob[]
  workspace_loads?: WorkspaceLoad[]
  recent_terminations: RecentTermination[]
  /** The caller's held requests, keyed by thread id. */
  pending_requests?: Record<string, PendingRequest>
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

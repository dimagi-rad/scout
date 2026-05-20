import { api } from "@/api/client"

export type JobState = "pending" | "running" | "completed" | "failed" | "cancelled"

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
  job_type: "materialization"
  state: JobState
  progress: JobProgress | null
  created_at: string
}

export const jobsApi = {
  active: (workspaceId: string) =>
    api.get<{ jobs: ActiveJob[] }>(`/api/workspaces/${workspaceId}/jobs/active/`),
  cancel: (workspaceId: string, threadJobId: string) =>
    api.post<void>(`/api/workspaces/${workspaceId}/jobs/${threadJobId}/cancel/`, {}),
}

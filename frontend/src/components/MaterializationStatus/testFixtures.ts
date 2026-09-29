import type { ActiveJob, RecentTermination } from "@/api/jobs"

export const WORKSPACE_ID = "ws-1"

export const job: ActiveJob = {
  thread_job_id: "job-1",
  thread_id: "thread-1",
  tool_call_id: "call-1",
  job_type: "materialization",
  state: "running",
  progress: null,
  created_at: "2026-09-23T10:00:00Z",
}

export const termination: RecentTermination = {
  thread_job_id: "job-1",
  thread_id: "thread-1",
  tool_call_id: "call-1",
  state: "failed",
  completed_at: "2026-09-23T10:05:00Z",
  error_summary: "Upstream timed out",
  retry_available: true,
}

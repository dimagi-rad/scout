export interface QueryResult {
  name: string
  semantic_query?: Record<string, unknown>
  columns?: string[]
  rows?: unknown[][]
  row_count?: number
  truncated?: boolean
  error?: string
}

export interface QueryDataResponse {
  queries: QueryResult[]
  static_data: Record<string, unknown>
  semantic_query_manifest?: Record<string, unknown>
}

export type ArtifactDataStatus =
  | "not_required"
  | "ready"
  | "needs_materialization"
  | "needs_semantic_rebuild"
  | "recovering"
  | "failed"
  | "unavailable"

export type ArtifactRecoveryAction = "materialization" | "semantic_rebuild" | null

export interface ArtifactRecoveryProgress {
  percent: number | null
  rows_loaded: number
  rows_total: number | null
  unit: string
  message: string | null
  source: string | null
  step: number | null
  total_steps: number | null
}

export interface ArtifactRecoveryJob {
  id: string | null
  type: Exclude<ArtifactRecoveryAction, null>
  state: "pending" | "running" | "completed" | "failed"
  progress: ArtifactRecoveryProgress | null
  created_at: string | null
  started_at?: string | null
  completed_at?: string | null
}

export interface ArtifactDataRecoveryState {
  status: ArtifactDataStatus
  recovery_action: ArtifactRecoveryAction
  physical_status: string
  semantic_status: string
  message: string
  detail?: string
  can_retry: boolean
  recovery: ArtifactRecoveryJob | null
}

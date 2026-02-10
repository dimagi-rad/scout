/**
 * TypeScript types for data sources API.
 */

export interface DatabaseConnection {
  id: string
  name: string
  description: string
  db_host: string
  db_port: number
  db_name: string
  is_active: boolean
  project_count: number
  created_at: string
  updated_at: string
}

export interface DatabaseConnectionFormData {
  name: string
  description: string
  db_host: string
  db_port: number
  db_name: string
  db_user?: string
  db_password?: string
  is_active: boolean
}

export interface DataSource {
  id: string
  name: string
  source_type: DataSourceType
  source_type_display: string
  base_url: string
  oauth_client_id: string
  config: Record<string, unknown>
  is_active: boolean
  created_at: string
  updated_at: string
}

export type DataSourceType = "commcare" | "commcare_connect"

export interface DataSourceFormData {
  name: string
  source_type: DataSourceType
  base_url: string
  oauth_client_id: string
  oauth_client_secret?: string
  config: Record<string, unknown>
  is_active: boolean
}

export interface ProjectDataSource {
  id: string
  project: string
  data_source: string
  data_source_name: string
  data_source_type: DataSourceType
  credential_mode: CredentialMode
  credential_mode_display: string
  sync_config: Record<string, unknown>
  refresh_interval_hours: number
  is_active: boolean
  created_at: string
  updated_at: string
}

export type CredentialMode = "project" | "user"

export interface DataSourceCredential {
  id: string
  data_source: string
  data_source_name: string
  data_source_type: DataSourceType
  project: string | null
  user: number | null
  token_expires_at: string
  is_valid: boolean
  created_at: string
  updated_at: string
}

export interface MaterializedDataset {
  id: string
  project_data_source: string
  data_source_name: string
  data_source_type: DataSourceType
  user: number | null
  schema_name: string
  status: DatasetStatus
  status_display: string
  last_sync_at: string | null
  last_activity_at: string
  row_counts: Record<string, number>
  created_at: string
  updated_at: string
}

export type DatasetStatus = "pending" | "syncing" | "ready" | "error" | "stale"

export interface SyncJob {
  id: string
  materialized_dataset: string
  status: SyncJobStatus
  status_display: string
  started_at: string | null
  completed_at: string | null
  progress: Record<string, unknown>
  error_message: string | null
  resume_after: string | null
  created_at: string
}

export type SyncJobStatus = "pending" | "running" | "completed" | "failed" | "paused"

export interface DataSourceTypeOption {
  value: DataSourceType
  label: string
}

export interface ConnectionTestResult {
  success: boolean
  schemas?: string[]
  error?: string
}

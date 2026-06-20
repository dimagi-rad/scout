import { api } from "@/api/client"

export interface DashboardResponse {
  sql: string
  columns: string[]
  rows: (string | number | null)[][]
  row_count: number
}

export type MappingStatus = "resolved" | "low_confidence" | "absent"

export interface OppMapping {
  opportunity_id: string
  status: MappingStatus
  confidence: number
  column: string
  source_path: string
  matched_label: string
  sql_expression: string
}

export interface MeasureLineage {
  measure: string
  coverage: { resolved: number; low_confidence: number; absent: number; total: number }
  opps: OppMapping[]
}

export interface InspectorResponse {
  workspace_id: string
  schema_name: string
  measures: MeasureLineage[]
  model_yaml: string
}

export interface ApproveResponse {
  status: string
  measure: string
  lineage: unknown[]
}

export const approveMeasure = (
  workspaceId: string,
  draftId: string,
  overrides: Record<string, { action: "confirm" | "pick" | "reject"; column?: string }>,
) =>
  api.post<ApproveResponse>(
    `/api/workspaces/${workspaceId}/crossopp/measures/${draftId}/approve/`,
    { overrides },
  )

export const crossOppApi = {
  dashboard: (workspaceId: string) =>
    api.get<DashboardResponse>(`/api/workspaces/${workspaceId}/crossopp/dashboard/`),
  inspector: (workspaceId: string) =>
    api.get<InspectorResponse>(`/api/workspaces/${workspaceId}/crossopp/inspector/`),
  approveMeasure,
}

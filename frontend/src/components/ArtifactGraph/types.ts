export interface ArtifactDetail {
  id: string
  title: string
  type: string
  code: string
  data: Record<string, unknown>
  semantic_queries: Array<Record<string, unknown>>
  semantic_query_manifest?: Record<string, unknown>
  version: number
}

export interface StoryDoc {
  schema_version?: number
  version?: number
  name?: string
  prd?: string
  blocks: StoryBlock[]
}

export interface StoryBlock {
  id: string
  type: string
  hidden?: boolean
  row_group?: string
  inputs?: Record<string, Binding>
  config?: Record<string, unknown>
}

export type Binding = { $ref: string } | { value: unknown }

export interface DateRange {
  start: string
  end: string
  preset?: string
}

export interface CompareRanges {
  current: DateRange
  previous: DateRange
  label?: string
}

export type Row = Record<string, unknown>

export interface SemanticQuerySpec {
  measures?: string[]
  dimensions?: string[]
  time_dimension?: string
  granularity?: string
  filters?: Array<{ field: string; operator?: string; value?: unknown; values?: unknown[] }>
  order_by?: Array<{ field: string; direction?: string }>
  limit?: number
}

export interface ResolvedQuery extends SemanticQuerySpec {
  date_range?: DateRange
}

export interface OutputState {
  status: "idle" | "pending" | "ready" | "error"
  value?: unknown
  error?: string
}

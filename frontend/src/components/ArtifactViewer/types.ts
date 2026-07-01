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

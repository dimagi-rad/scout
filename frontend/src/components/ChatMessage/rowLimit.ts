interface RowLimitedData {
  row_count: number
  truncated?: boolean
  row_limit?: number
  // Set when the byte budget (apps/agents/tool_results.py) cut rows further.
  truncation?: { original_row_count?: number }
}

// The chat row limit a result was cut off at, or null. A result that stopped at
// a smaller limit the agent chose is complete as asked, so it gets no notice.
export function rowLimitCutOff(data: RowLimitedData | undefined): number | null {
  if (!data?.truncated || data.row_limit == null) return null
  const returned = data.truncation?.original_row_count ?? data.row_count
  return returned < data.row_limit ? null : data.row_limit
}

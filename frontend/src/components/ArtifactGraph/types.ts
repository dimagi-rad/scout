import type React from "react"

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

export function isRefBinding(binding: Binding): binding is { $ref: string } {
  const value = binding as { $ref?: unknown; value?: unknown } | null
  return typeof value === "object" && value !== null && typeof value.$ref === "string" && !("value" in value)
}

export function isLiteralBinding(binding: Binding): binding is { value: unknown } {
  const value = binding as { $ref?: unknown; value?: unknown } | null
  return typeof value === "object" && value !== null && "value" in value && !("$ref" in value)
}

export type PortType = "date_range" | "compare_ranges" | "rows" | "scalar" | "text" | "json"

export interface PortDecl {
  name: string
  type: PortType
  required?: boolean
}

export interface BlockPorts {
  inputs: PortDecl[]
  outputs: PortDecl[]
}

export type OutputStatus = "idle" | "pending" | "ready" | "error" | "blocked"

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
  status: OutputStatus
  value?: unknown
  error?: string
  epoch?: number
}

export interface Diagnostic {
  severity: "error" | "warning"
  blockId?: string
  message: string
}

export interface StoryRuntimeContext {
  runQuery: (query: ResolvedQuery, options: { signal?: AbortSignal }) => Promise<Row[]>
}

export interface EvaluateArgs<Config = Record<string, unknown>> {
  blockId: string
  config: Config
  inputs: Record<string, unknown>
  ctx: StoryRuntimeContext
  signal: AbortSignal
}

export interface StoryEngineApi {
  getOutput: (ref: string) => OutputState
  subscribe: (ref: string, callback: () => void) => () => void
  subscribeAll: (callback: () => void) => () => void
  setSourceOutputs: (blockId: string, outputs: Record<string, unknown>) => void
  getDiagnostics: () => Diagnostic[]
}

export interface BlockComponentProps<Config = Record<string, unknown>> {
  block: StoryBlock
  config: Config
  engine: StoryEngineApi
}

export interface BlockSpec<Config = Record<string, unknown>> {
  type: string
  displayName: string
  kind: "source" | "compute" | "visual"
  hiddenByDefault?: boolean
  debounceMs?: number
  ports: (config: Config, bindings?: Record<string, Binding>) => BlockPorts
  initialOutputs?: (config: Config) => Record<string, unknown>
  evaluate?: (args: EvaluateArgs<Config>) => Promise<Record<string, unknown>>
  component?: React.ComponentType<BlockComponentProps<Config>>
}

export function outputKey(blockId: string, port: string): string {
  return `${blockId}.${port}`
}

export function parseRef(ref: string): { blockId: string; port: string } | null {
  const index = ref.indexOf(".")
  if (index <= 0 || index === ref.length - 1) return null
  return { blockId: ref.slice(0, index), port: ref.slice(index + 1) }
}

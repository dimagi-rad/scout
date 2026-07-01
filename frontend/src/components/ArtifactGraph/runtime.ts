import { api } from "@/api/client"

import type { DateRange, ResolvedQuery, Row, SemanticQuerySpec, StoryDoc } from "./types"

interface SemanticQueryResponse {
  columns?: string[]
  rows?: unknown[]
  row_count?: number
  truncated?: boolean
  semantic_query?: Record<string, unknown>
}

export function normalizeStoryDoc(value: unknown, fallbackName: string): StoryDoc {
  const record = isRecord(value) ? value : {}
  const blocks = Array.isArray(record.blocks) ? record.blocks.filter(isRecord) : []
  return {
    schema_version: numberValue(record.schema_version) ?? numberValue(record.version) ?? 1,
    name: stringValue(record.name) ?? fallbackName,
    prd: stringValue(record.prd),
    blocks: blocks.map((block, index) => ({
      id: stringValue(block.id) ?? `block_${index}`,
      type: stringValue(block.type) ?? "markdown",
      hidden: Boolean(block.hidden),
      row_group: stringValue(block.row_group),
      inputs: normalizeInputs(block.inputs),
      config: isRecord(block.config) ? block.config : {},
    })),
  }
}

export function resolvePresetRange(preset: string | undefined, today = new Date()): DateRange {
  const end = startOfDay(today)
  const start = new Date(end)
  switch (preset) {
    case "today":
      break
    case "yesterday":
      start.setDate(start.getDate() - 1)
      end.setDate(end.getDate() - 1)
      break
    case "last_7_days":
      start.setDate(start.getDate() - 6)
      break
    case "last_90_days":
      start.setDate(start.getDate() - 89)
      break
    case "month_to_date":
      start.setDate(1)
      break
    default:
      start.setDate(start.getDate() - 29)
      preset = "last_30_days"
      break
  }
  return { start: isoDate(start), end: isoDate(end), preset }
}

export function previousPeriod(range: DateRange): DateRange {
  const start = parseIsoDate(range.start)
  const end = parseIsoDate(range.end)
  const days = Math.max(1, Math.round((end.getTime() - start.getTime()) / 86_400_000) + 1)
  start.setDate(start.getDate() - days)
  end.setDate(end.getDate() - days)
  return { start: isoDate(start), end: isoDate(end), preset: "previous_period" }
}

export function buildSemanticQueryInput(query: ResolvedQuery): SemanticQuerySpec {
  if (query.date_range && !query.time_dimension) {
    throw new Error("A date_range binding requires time_dimension")
  }
  const filters = [...(query.filters ?? [])]
  if (query.date_range && query.time_dimension) {
    filters.push({
      field: query.time_dimension,
      operator: "inDateRange",
      values: [query.date_range.start, query.date_range.end],
    })
  }
  return {
    measures: query.measures ?? [],
    dimensions: query.dimensions ?? [],
    time_dimension: query.time_dimension,
    granularity: query.granularity,
    filters,
    order_by: query.order_by,
    limit: query.limit,
  }
}

export async function runSemanticQuery(
  workspaceId: string,
  query: ResolvedQuery,
): Promise<Row[]> {
  const response = await api.post<SemanticQueryResponse>(
    `/api/workspaces/${workspaceId}/semantic-query/`,
    buildSemanticQueryInput(query),
  )
  return normalizeResultRows(response.rows ?? [], response.columns ?? [], query)
}

export function normalizeResultRows(rows: unknown[], columns: string[], query: ResolvedQuery): Row[] {
  if (!Array.isArray(rows)) return []
  return rows.map((row) => {
    if (Array.isArray(row)) {
      return Object.fromEntries(columns.map((column, index) => [normalizeKey(column, query), row[index]]))
    }
    if (isRecord(row)) {
      return Object.fromEntries(
        Object.entries(row).map(([key, value]) => [normalizeKey(key, query), value]),
      )
    }
    return {}
  })
}

export function normalizeKey(key: string, query: ResolvedQuery): string {
  const normalized = key.replace(/\./g, "_").replace(/__+/g, "_")
  if (query.time_dimension && query.granularity) {
    const timeKey = query.time_dimension.replace(/\./g, "_")
    if (normalized === "date" || normalized === timeKey || normalized === `${timeKey}_${query.granularity}`) {
      return "date"
    }
  }
  return normalized
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

export function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined
}

export function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

function normalizeInputs(value: unknown): StoryDoc["blocks"][number]["inputs"] {
  if (!isRecord(value)) return undefined
  const inputs: StoryDoc["blocks"][number]["inputs"] = {}
  for (const [key, binding] of Object.entries(value)) {
    if (!isRecord(binding)) continue
    if (typeof binding.$ref === "string" && !("value" in binding)) {
      inputs[key] = { $ref: binding.$ref }
    } else if ("value" in binding && !("$ref" in binding)) {
      inputs[key] = { value: binding.value }
    }
  }
  return inputs
}

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate())
}

function isoDate(date: Date): string {
  return date.toISOString().slice(0, 10)
}

function parseIsoDate(value: string): Date {
  const [year, month, day] = value.split("-").map((part) => Number.parseInt(part, 10))
  return new Date(year, month - 1, day)
}

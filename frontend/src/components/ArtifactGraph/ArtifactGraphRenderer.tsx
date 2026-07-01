import { useEffect, useMemo, useState } from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"

import { cn } from "@/lib/utils"

import {
  buildSemanticQueryInput,
  isRecord,
  normalizeStoryDoc,
  previousPeriod,
  resolvePresetRange,
  runSemanticQuery,
  stringValue,
} from "./runtime"
import type {
  ArtifactDetail,
  Binding,
  CompareRanges,
  DateRange,
  OutputState,
  ResolvedQuery,
  Row,
  SemanticQuerySpec,
  StoryBlock,
} from "./types"

interface ArtifactGraphRendererProps {
  artifact: ArtifactDetail
  workspaceId: string
}

interface QueryTask {
  outputRef: string
  query: ResolvedQuery
}

const PALETTE = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#dc2626"]

export function ArtifactGraphRenderer({ artifact, workspaceId }: ArtifactGraphRendererProps) {
  const doc = useMemo(
    () => normalizeStoryDoc(isRecord(artifact.data) ? artifact.data.story_doc : undefined, artifact.title),
    [artifact.data, artifact.title],
  )
  const docSignature = useMemo(() => JSON.stringify(doc), [doc])
  const [sourceValues, setSourceValues] = useState<Record<string, unknown>>({})
  const sourceOutputs = useMemo(() => buildSourceOutputs(doc.blocks, sourceValues), [doc.blocks, sourceValues])
  const [queryOutputs, setQueryOutputs] = useState<Record<string, OutputState>>({})
  const outputs = useMemo(
    () => ({ ...sourceOutputs, ...queryOutputs }),
    [sourceOutputs, queryOutputs],
  )
  const sourceSignature = useMemo(() => JSON.stringify(sourceOutputs), [sourceOutputs])

  useEffect(() => {
    setSourceValues((current) => initializeSourceValues(doc.blocks, current))
  }, [docSignature, doc.blocks])

  useEffect(() => {
    const tasks = collectQueryTasks(doc.blocks, sourceOutputs)
    const outputRefs = tasks.map((task) => task.outputRef)
    if (outputRefs.length === 0) {
      setQueryOutputs({})
      return
    }
    let cancelled = false
    setQueryOutputs((current) => {
      const next = { ...current }
      for (const ref of outputRefs) {
        next[ref] = { status: "pending", value: current[ref]?.value }
      }
      return next
    })
    void Promise.all(
      tasks.map(async (task) => {
        try {
          buildSemanticQueryInput(task.query)
          const rows = await runSemanticQuery(workspaceId, task.query)
          return { ref: task.outputRef, state: { status: "ready" as const, value: rows } }
        } catch (error) {
          return {
            ref: task.outputRef,
            state: {
              status: "error" as const,
              error: error instanceof Error ? error.message : "Query failed",
            },
          }
        }
      }),
    ).then((results) => {
      if (cancelled) return
      setQueryOutputs((current) => {
        const next = { ...current }
        for (const result of results) {
          next[result.ref] = result.state
        }
        return next
      })
    })
    return () => {
      cancelled = true
    }
  }, [doc.blocks, sourceOutputs, sourceSignature, workspaceId])

  const visibleGroups = useMemo(
    () => groupVisibleBlocks(doc.blocks.filter((block) => !block.hidden)),
    [doc.blocks],
  )

  return (
    <div className="h-full overflow-y-auto bg-background">
      <div className="mx-auto max-w-5xl px-6 py-6">
        {doc.prd && (
          <div className="mb-5 border-l-2 border-primary/40 pl-3 text-xs text-muted-foreground">
            <Markdown remarkPlugins={[remarkGfm]}>{doc.prd}</Markdown>
          </div>
        )}
        <div className="space-y-4">
          {visibleGroups.map((group, index) => (
            <div
              key={`${group.key}-${index}`}
              className={cn(group.blocks.length > 1 && "grid gap-4 md:grid-cols-2")}
            >
              {group.blocks.map((block) => (
                <GraphBlock
                  key={block.id}
                  block={block}
                  outputs={outputs}
                  sourceValues={sourceValues}
                  onSourceChange={(value) =>
                    setSourceValues((current) => ({ ...current, [block.id]: value }))
                  }
                />
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function GraphBlock({
  block,
  outputs,
  sourceValues,
  onSourceChange,
}: {
  block: StoryBlock
  outputs: Record<string, OutputState>
  sourceValues: Record<string, unknown>
  onSourceChange: (value: unknown) => void
}) {
  const config = block.config ?? {}
  if (block.type === "title") {
    return (
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-normal">{stringValue(config.text) ?? "Untitled"}</h1>
        {stringValue(config.subtitle) && <p className="text-sm text-muted-foreground">{stringValue(config.subtitle)}</p>}
      </header>
    )
  }
  if (block.type === "section") {
    return (
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">{stringValue(config.title) ?? "Section"}</h2>
        <div className="prose prose-sm max-w-none dark:prose-invert">
          <Markdown remarkPlugins={[remarkGfm]}>{stringValue(config.body) ?? ""}</Markdown>
        </div>
      </section>
    )
  }
  if (block.type === "question") {
    return <div className="text-base font-medium">{stringValue(config.text) ?? ""}</div>
  }
  if (block.type === "tldr") {
    return <TldrBlock config={config} />
  }
  if (block.type === "markdown") {
    return (
      <div className="prose prose-sm max-w-none dark:prose-invert">
        <Markdown remarkPlugins={[remarkGfm]}>
          {stringValue(config.body) ?? stringValue(config.content) ?? ""}
        </Markdown>
      </div>
    )
  }
  if (block.type === "date_filter") {
    return (
      <DateFilterBlock
        config={config}
        value={(sourceValues[block.id] as DateRange | undefined) ?? resolvePresetRange(stringValue(config.default))}
        onChange={onSourceChange}
      />
    )
  }
  if (block.type === "period_selector") {
    return (
      <PeriodSelectorBlock
        config={config}
        value={(sourceValues[block.id] as DateRange | undefined) ?? resolvePresetRange(stringValue(config.default_range) ?? "last_30_days")}
        onChange={onSourceChange}
      />
    )
  }
  if (block.type === "graph") {
    return <GraphVisual block={block} state={dataStateForBlock(block, outputs)} />
  }
  if (block.type === "table") {
    return <TableVisual block={block} state={dataStateForBlock(block, outputs)} />
  }
  if (block.type === "stat") {
    return <StatVisual block={block} state={inputState(block, "current", outputs)} />
  }
  return null
}

function TldrBlock({ config }: { config: Record<string, unknown> }) {
  const items = Array.isArray(config.items) ? config.items : []
  if (items.length > 0) {
    return (
      <div className="space-y-2 border-y border-border py-3">
        {items.map((item, index) => (
          <div key={index} className="text-sm">
            {String(item)}
          </div>
        ))}
      </div>
    )
  }
  return (
    <div className="border-y border-border py-3 text-sm font-medium">
      {stringValue(config.content) ?? ""}
    </div>
  )
}

function DateFilterBlock({
  config,
  value,
  onChange,
}: {
  config: Record<string, unknown>
  value: DateRange
  onChange: (value: DateRange) => void
}) {
  return (
    <div className="flex flex-wrap items-end gap-2 border-y border-border py-3">
      <label className="grid gap-1 text-xs font-medium text-muted-foreground">
        {stringValue(config.label) ?? "Date range"}
        <select
          className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground"
          value={value.preset ?? "custom"}
          onChange={(event) => onChange(resolvePresetRange(event.target.value))}
        >
          <option value="last_30_days">Last 30 days</option>
          <option value="last_7_days">Last 7 days</option>
          <option value="last_90_days">Last 90 days</option>
          <option value="month_to_date">Month to date</option>
          <option value="today">Today</option>
          <option value="yesterday">Yesterday</option>
        </select>
      </label>
      <DateInput label="Start" value={value.start} onChange={(start) => onChange({ ...value, start, preset: "custom" })} />
      <DateInput label="End" value={value.end} onChange={(end) => onChange({ ...value, end, preset: "custom" })} />
    </div>
  )
}

function PeriodSelectorBlock({
  config,
  value,
  onChange,
}: {
  config: Record<string, unknown>
  value: DateRange
  onChange: (value: DateRange) => void
}) {
  const previous = previousPeriod(value)
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-y border-border py-3">
      <label className="grid gap-1 text-xs font-medium text-muted-foreground">
        {stringValue(config.label) ?? "Period"}
        <select
          className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground"
          value={value.preset ?? "last_30_days"}
          onChange={(event) => onChange(resolvePresetRange(event.target.value))}
        >
          <option value="last_7_days">Last 7 days</option>
          <option value="last_30_days">Last 30 days</option>
          <option value="last_90_days">Last 90 days</option>
          <option value="month_to_date">Month to date</option>
        </select>
      </label>
      <div className="text-xs text-muted-foreground">
        Current {value.start} to {value.end}; previous {previous.start} to {previous.end}
      </div>
    </div>
  )
}

function DateInput({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="grid gap-1 text-xs font-medium text-muted-foreground">
      {label}
      <input
        type="date"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground"
      />
    </label>
  )
}

function GraphVisual({ block, state }: { block: StoryBlock; state: OutputState }) {
  const rows = rowsFromState(state)
  const config = block.config ?? {}
  const title = stringValue(config.title)
  const chartType = stringValue(config.chart_type) ?? "line"
  const xKey = stringValue(config.x_key) ?? "date"
  const series = seriesKeys(config, rows, xKey)
  return (
    <div className="space-y-2">
      {title && <h3 className="text-sm font-semibold">{title}</h3>}
      <OutputStatus state={state} />
      {rows.length > 0 && series.length > 0 ? (
        <MiniChart rows={rows} xKey={xKey} series={series} chartType={chartType} />
      ) : (
        state.status === "ready" && <div className="py-8 text-center text-sm text-muted-foreground">No data</div>
      )}
    </div>
  )
}

function MiniChart({
  rows,
  xKey,
  series,
  chartType,
}: {
  rows: Row[]
  xKey: string
  series: string[]
  chartType: string
}) {
  const width = 560
  const height = 240
  const padding = 36
  const values = rows.flatMap((row) => series.map((key) => numeric(row[key]))).filter((value) => value !== null)
  const max = Math.max(...values, 1)
  const min = Math.min(...values, 0)
  const span = Math.max(max - min, 1)
  const x = (index: number) => padding + (index * (width - padding * 2)) / Math.max(rows.length - 1, 1)
  const y = (value: number) => height - padding - ((value - min) / span) * (height - padding * 2)
  return (
    <div className="overflow-hidden rounded-md border border-border">
      <svg viewBox={`0 0 ${width} ${height}`} className="h-64 w-full bg-background">
        <line x1={padding} x2={width - padding} y1={height - padding} y2={height - padding} stroke="#d1d5db" />
        <line x1={padding} x2={padding} y1={padding} y2={height - padding} stroke="#d1d5db" />
        {chartType === "bar"
          ? series.map((key, seriesIndex) =>
              rows.map((row, rowIndex) => {
                const value = numeric(row[key]) ?? 0
                const barWidth = Math.max(4, (width - padding * 2) / Math.max(rows.length, 1) / (series.length + 0.5))
                const barX = padding + rowIndex * ((width - padding * 2) / Math.max(rows.length, 1)) + seriesIndex * barWidth
                const barY = y(value)
                return (
                  <rect
                    key={`${key}-${rowIndex}`}
                    x={barX}
                    y={barY}
                    width={barWidth}
                    height={height - padding - barY}
                    fill={PALETTE[seriesIndex % PALETTE.length]}
                  />
                )
              }),
            )
          : series.map((key, seriesIndex) => {
              const points = rows
                .map((row, rowIndex) => {
                  const value = numeric(row[key])
                  return value === null ? null : `${x(rowIndex)},${y(value)}`
                })
                .filter(Boolean)
                .join(" ")
              return (
                <polyline
                  key={key}
                  points={points}
                  fill="none"
                  stroke={PALETTE[seriesIndex % PALETTE.length]}
                  strokeWidth={2.5}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              )
            })}
        {rows.length > 0 && (
          <>
            <text x={padding} y={height - 10} className="fill-muted-foreground text-[10px]">
              {String(rows[0][xKey] ?? "")}
            </text>
            <text x={width - padding} y={height - 10} textAnchor="end" className="fill-muted-foreground text-[10px]">
              {String(rows[rows.length - 1][xKey] ?? "")}
            </text>
          </>
        )}
      </svg>
      <div className="flex flex-wrap gap-3 border-t border-border px-3 py-2 text-xs">
        {series.map((key, index) => (
          <span key={key} className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: PALETTE[index % PALETTE.length] }} />
            {key}
          </span>
        ))}
      </div>
    </div>
  )
}

function TableVisual({ block, state }: { block: StoryBlock; state: OutputState }) {
  const rows = rowsFromState(state)
  const config = block.config ?? {}
  const columns = tableColumns(config, rows)
  return (
    <div className="space-y-2">
      {stringValue(config.title) && <h3 className="text-sm font-semibold">{stringValue(config.title)}</h3>}
      <OutputStatus state={state} />
      {rows.length > 0 && columns.length > 0 && (
        <div className="max-h-96 overflow-auto rounded-md border border-border">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-muted">
              <tr>
                {columns.map((column) => (
                  <th key={column.key} className="px-3 py-2 text-left font-medium text-muted-foreground">
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.slice(0, 100).map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {columns.map((column) => (
                    <td key={column.key} className="px-3 py-1.5">
                      {formatValue(row[column.key])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function StatVisual({ block, state }: { block: StoryBlock; state: OutputState }) {
  const rows = rowsFromState(state)
  const config = block.config ?? {}
  const key = stringValue(config.value_key) ?? pathKey(stringValue(config.value_path)) ?? firstNumericKey(rows[0])
  const value = key ? rows[0]?.[key] : undefined
  return (
    <div className="space-y-1 border-y border-border py-4">
      <div className="text-xs font-medium uppercase tracking-normal text-muted-foreground">
        {stringValue(config.label) ?? stringValue(config.title) ?? key ?? "Value"}
      </div>
      <OutputStatus state={state} />
      <div className="text-3xl font-semibold tabular-nums">{formatValue(value)}</div>
    </div>
  )
}

function OutputStatus({ state }: { state: OutputState }) {
  if (state.status === "pending") {
    return <div className="text-xs text-muted-foreground">Loading data...</div>
  }
  if (state.status === "error") {
    return <div className="text-xs text-destructive">{state.error ?? "Data failed to load"}</div>
  }
  return null
}

function initializeSourceValues(blocks: StoryBlock[], current: Record<string, unknown>) {
  let changed = false
  const next = { ...current }
  for (const block of blocks) {
    if (block.type === "date_filter" && !next[block.id]) {
      next[block.id] = resolvePresetRange(stringValue(block.config?.default))
      changed = true
    }
    if (block.type === "period_selector" && !next[block.id]) {
      next[block.id] = resolvePresetRange(stringValue(block.config?.default_range) ?? "last_30_days")
      changed = true
    }
  }
  return changed ? next : current
}

function buildSourceOutputs(blocks: StoryBlock[], sourceValues: Record<string, unknown>): Record<string, OutputState> {
  const outputs: Record<string, OutputState> = {}
  for (const block of blocks) {
    if (block.type === "date_filter") {
      outputs[`${block.id}.value`] = {
        status: "ready",
        value: sourceValues[block.id] ?? resolvePresetRange(stringValue(block.config?.default)),
      }
    }
    if (block.type === "period_selector") {
      const current = (sourceValues[block.id] as DateRange | undefined) ?? resolvePresetRange(stringValue(block.config?.default_range) ?? "last_30_days")
      const previous = previousPeriod(current)
      const pair: CompareRanges = { current, previous, label: "Previous period" }
      outputs[`${block.id}.current`] = { status: "ready", value: current }
      outputs[`${block.id}.previous`] = { status: "ready", value: previous }
      outputs[`${block.id}.pair`] = { status: "ready", value: pair }
    }
  }
  return outputs
}

function collectQueryTasks(blocks: StoryBlock[], outputs: Record<string, OutputState>): QueryTask[] {
  const tasks: QueryTask[] = []
  for (const block of blocks) {
    const config = block.config ?? {}
    if (block.type === "semantic_query" && isRecord(config.queries)) {
      const compare = config.compare === true ? asCompare(resolveInputValue(block, "compare", outputs)) : undefined
      const dateRange = asDateRange(resolveInputValue(block, "date_range", outputs))
      for (const [name, query] of Object.entries(config.queries)) {
        if (!isSemanticQuerySpec(query)) continue
        if (compare) {
          tasks.push({ outputRef: `${block.id}.${name}`, query: { ...query, date_range: compare.current } })
          tasks.push({ outputRef: `${block.id}.${name}_previous`, query: { ...query, date_range: compare.previous } })
        } else {
          tasks.push({ outputRef: `${block.id}.${name}`, query: { ...query, date_range: dateRange } })
        }
      }
    }
    if ((block.type === "graph" || block.type === "table") && isSemanticQuerySpec(config.query)) {
      tasks.push({
        outputRef: `${block.id}.data`,
        query: { ...config.query, date_range: asDateRange(resolveInputValue(block, "date_range", outputs)) },
      })
    }
  }
  return tasks
}

function resolveInputValue(block: StoryBlock, inputName: string, outputs: Record<string, OutputState>): unknown {
  const binding = block.inputs?.[inputName] as Binding | undefined
  if (!binding) return undefined
  if ("value" in binding) return binding.value
  const state = outputs[binding.$ref]
  return state?.status === "ready" ? state.value : undefined
}

function dataStateForBlock(block: StoryBlock, outputs: Record<string, OutputState>): OutputState {
  const binding = block.inputs?.data as Binding | undefined
  if (binding && "$ref" in binding) {
    return outputs[binding.$ref] ?? { status: "idle" }
  }
  if (isSemanticQuerySpec(block.config?.query)) {
    return outputs[`${block.id}.data`] ?? { status: "pending" }
  }
  return { status: "idle" }
}

function inputState(block: StoryBlock, inputName: string, outputs: Record<string, OutputState>): OutputState {
  const binding = block.inputs?.[inputName] as Binding | undefined
  if (binding && "$ref" in binding) {
    return outputs[binding.$ref] ?? { status: "idle" }
  }
  if (binding && "value" in binding) {
    return { status: "ready", value: binding.value }
  }
  return { status: "idle" }
}

function groupVisibleBlocks(blocks: StoryBlock[]) {
  const groups: Array<{ key: string; blocks: StoryBlock[] }> = []
  for (const block of blocks) {
    const key = block.row_group || block.id
    const last = groups[groups.length - 1]
    if (block.row_group && last?.key === block.row_group) {
      last.blocks.push(block)
    } else {
      groups.push({ key, blocks: [block] })
    }
  }
  return groups
}

function rowsFromState(state: OutputState): Row[] {
  return state.value && Array.isArray(state.value) ? state.value.filter(isRecord) : []
}

function seriesKeys(config: Record<string, unknown>, rows: Row[], xKey: string): string[] {
  const series = config.series
  if (Array.isArray(series) && series.length > 0) {
    return series
      .map((item) => {
        if (typeof item === "string") return item
        if (isRecord(item)) return stringValue(item.data_key) ?? stringValue(item.y_key) ?? stringValue(item.key)
        return undefined
      })
      .filter((item): item is string => Boolean(item))
  }
  const yKey = stringValue(config.y_key)
  if (yKey) return [yKey]
  return Object.keys(rows[0] ?? {}).filter((key) => key !== xKey && numeric(rows[0][key]) !== null)
}

function tableColumns(config: Record<string, unknown>, rows: Row[]): Array<{ key: string; label: string }> {
  const columns = config.columns
  if (Array.isArray(columns) && columns.length > 0) {
    return columns
      .map((item) => {
        if (typeof item === "string") return { key: item, label: item }
        if (isRecord(item)) {
          const key = stringValue(item.key) ?? stringValue(item.accessor)
          return key ? { key, label: stringValue(item.label) ?? key } : undefined
        }
        return undefined
      })
      .filter((item): item is { key: string; label: string } => Boolean(item))
  }
  return Object.keys(rows[0] ?? {}).map((key) => ({ key, label: key }))
}

function isSemanticQuerySpec(value: unknown): value is SemanticQuerySpec {
  return isRecord(value)
}

function asDateRange(value: unknown): DateRange | undefined {
  return isRecord(value) && typeof value.start === "string" && typeof value.end === "string"
    ? { start: value.start, end: value.end, preset: stringValue(value.preset) }
    : undefined
}

function asCompare(value: unknown): CompareRanges | undefined {
  if (!isRecord(value)) return undefined
  const current = asDateRange(value.current)
  const previous = asDateRange(value.previous)
  return current && previous ? { current, previous, label: stringValue(value.label) } : undefined
}

function numeric(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function firstNumericKey(row: Row | undefined): string | undefined {
  return row ? Object.keys(row).find((key) => numeric(row[key]) !== null) : undefined
}

function pathKey(value: string | undefined): string | undefined {
  return value?.match(/[A-Za-z_][A-Za-z0-9_]*/g)?.at(-1)
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "-"
  if (typeof value === "number") return new Intl.NumberFormat().format(value)
  return String(value)
}

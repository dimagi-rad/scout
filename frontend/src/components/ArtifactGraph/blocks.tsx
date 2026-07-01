import { CalendarDays, CalendarRange } from "lucide-react"
import type React from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ResponsiveContainer } from "recharts"

import { cn } from "@/lib/utils"

import { firstNumericKey, formatValue, pathKey, selectPath } from "./format"
import { useBlockInputs, useOutput } from "./hooks"
import {
  buildRechartsTree,
  collectResultKeyRefs,
  compileCompactGraphConfig,
  normalizeGraphSeries,
  type RechartsNode,
} from "./recharts"
import {
  buildSemanticQueryInput,
  isRecord,
  previousPeriod,
  resolvePresetRange,
  stringValue,
} from "./runtime"
import {
  outputKey,
  type BlockComponentProps,
  type BlockPorts,
  type BlockSpec,
  type CompareRanges,
  type DateRange,
  type EvaluateArgs,
  type OutputState,
  type Row,
  type SemanticQuerySpec,
} from "./types"

const EMPTY_ROWS: Row[] = []

function BlockCard({
  title,
  children,
  className,
}: {
  title?: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <section className={cn("space-y-2", className)}>
      {title && <h3 className="text-sm font-semibold">{title}</h3>}
      {children}
    </section>
  )
}

function TitleComponent({ config }: BlockComponentProps) {
  return (
    <header className="space-y-1">
      <h1 className="text-2xl font-semibold tracking-normal">{stringValue(config.text) ?? "Untitled"}</h1>
      {stringValue(config.subtitle) && <p className="text-sm text-muted-foreground">{stringValue(config.subtitle)}</p>}
    </header>
  )
}

function SectionComponent({ config }: BlockComponentProps) {
  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold">{stringValue(config.title) ?? "Section"}</h2>
      <MarkdownBlockContent content={stringValue(config.body) ?? stringValue(config.text) ?? ""} />
    </section>
  )
}

function QuestionComponent({ config }: BlockComponentProps) {
  const question = stringValue(config.text) ?? stringValue(config.question)
  const context = stringValue(config.context)
  return (
    <section className="space-y-1 border-l-2 border-primary/30 pl-3">
      {question && <div className="text-base font-medium">{question}</div>}
      {context && <p className="text-sm text-muted-foreground">{context}</p>}
    </section>
  )
}

function TldrComponent({ config }: BlockComponentProps) {
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
  return <div className="border-y border-border py-3 text-sm font-medium">{stringValue(config.content) ?? ""}</div>
}

function MarkdownComponent({ config }: BlockComponentProps) {
  return <MarkdownBlockContent content={stringValue(config.body) ?? stringValue(config.content) ?? ""} />
}

function MarkdownBlockContent({ content }: { content: string }) {
  return (
    <div className="prose prose-sm max-w-none dark:prose-invert">
      <Markdown remarkPlugins={[remarkGfm]}>{content}</Markdown>
    </div>
  )
}

function DateFilterComponent({ block, config, engine }: BlockComponentProps) {
  const state = useOutput(engine, outputKey(block.id, "value"))
  const value = asDateRange(state.value) ?? resolvePresetRange(stringValue(config.default))

  return (
    <div data-block-type="date_filter" className="flex flex-wrap items-end gap-2 border-y border-border py-3">
      <label className="grid gap-1 text-xs font-medium text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <CalendarDays className="h-3.5 w-3.5" />
          {stringValue(config.label) ?? "Date range"}
        </span>
        <select
          className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground"
          value={value.preset ?? "custom"}
          onChange={(event) => engine.setSourceOutputs(block.id, { value: resolvePresetRange(event.target.value) })}
        >
          <option value="last_30_days">Last 30 days</option>
          <option value="last_7_days">Last 7 days</option>
          <option value="last_90_days">Last 90 days</option>
          <option value="month_to_date">Month to date</option>
          <option value="today">Today</option>
          <option value="yesterday">Yesterday</option>
        </select>
      </label>
      <DateInput
        label="Start"
        value={value.start}
        onChange={(start) => engine.setSourceOutputs(block.id, { value: { ...value, start, preset: "custom" } })}
      />
      <DateInput
        label="End"
        value={value.end}
        onChange={(end) => engine.setSourceOutputs(block.id, { value: { ...value, end, preset: "custom" } })}
      />
    </div>
  )
}

function PeriodSelectorComponent({ block, config, engine }: BlockComponentProps) {
  const currentState = useOutput(engine, outputKey(block.id, "current"))
  const value = asDateRange(currentState.value) ?? resolvePresetRange(stringValue(config.default_range) ?? "last_30_days")
  const previous = previousPeriod(value)

  return (
    <div data-block-type="period_selector" className="flex flex-wrap items-center justify-between gap-3 border-y border-border py-3">
      <label className="grid gap-1 text-xs font-medium text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <CalendarRange className="h-3.5 w-3.5" />
          {stringValue(config.label) ?? "Period"}
        </span>
        <select
          className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground"
          value={value.preset ?? "last_30_days"}
          onChange={(event) => publishPeriodOutputs(engine, block.id, event.target.value)}
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

function GraphComponent({ block, config, engine }: BlockComponentProps) {
  const state = useOutput(engine, outputKey(block.id, "data"))
  const rows = rowsFromState(state)
  const xKey = stringValue(config.x_key) ?? "date"
  const inferredSeries = inferSeries(config, rows, xKey)
  const chartConfig = {
    chart_type: stringValue(config.chart_type) ?? "line",
    x_key: xKey,
    y_key: stringValue(config.y_key),
    series: inferredSeries,
    data_label: stringValue(config.data_label),
    y_format: stringValue(config.y_format),
    stacked: config.stacked === true,
  }
  const tree = isRechartsNode(config.recharts) ? config.recharts : compileCompactGraphConfig(chartConfig)
  const height = typeof config.height === "number" && Number.isFinite(config.height) ? config.height : 280
  const missing = rows.length > 0 ? collectMissingKeys(tree, rows) : []

  return (
    <BlockCard title={stringValue(config.title)}>
      <OutputStatus state={state} />
      {missing.length > 0 && (
        <div className="rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-700">
          Not in the data: {missing.map((ref) => `${ref.where} "${ref.key}"`).join(", ")}
        </div>
      )}
      {rows.length > 0 ? (
        <GraphBuildBoundary rows={rows} tree={tree} height={height} />
      ) : (
        state.status === "ready" && <EmptyBlock label="No data" minHeight={height} />
      )}
    </BlockCard>
  )
}

function GraphBuildBoundary({ rows, tree, height }: { rows: Row[]; tree: RechartsNode; height: number }) {
  try {
    const chart = buildRechartsTree(tree, rows)
    return (
      <div data-block-type="graph" style={{ width: "100%", height }}>
        <ResponsiveContainer width="100%" height="100%">
          {chart}
        </ResponsiveContainer>
      </div>
    )
  } catch (error) {
    return (
      <div className="flex min-h-48 items-center justify-center rounded-md border border-destructive/30 px-4 text-sm text-destructive">
        Chart config error: {error instanceof Error ? error.message : String(error)}
      </div>
    )
  }
}

function TableComponent({ block, config, engine }: BlockComponentProps) {
  const state = useOutput(engine, outputKey(block.id, "data"))
  const rows = rowsFromState(state)
  const columns = tableColumns(config, rows)
  return (
    <BlockCard title={stringValue(config.title)}>
      <OutputStatus state={state} />
      {rows.length > 0 && columns.length > 0 ? (
        <div data-block-type="table" className="max-h-96 overflow-auto rounded-md border border-border">
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
                      {formatValue(row[column.key], column.format)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        state.status === "ready" && <EmptyBlock label="No data" minHeight={160} />
      )}
    </BlockCard>
  )
}

function StatComponent({ block, config, engine }: BlockComponentProps) {
  const inputs = useBlockInputs(engine, block)
  const rows = Array.isArray(inputs.values.current) ? inputs.values.current.filter(isRecord) : EMPTY_ROWS
  const key = stringValue(config.value_key) ?? pathKey(stringValue(config.value_path)) ?? firstNumericKey(rows[0])
  const selected = stringValue(config.value_path)
    ? selectPath(inputs.values.current, stringValue(config.value_path))
    : key
      ? rows[0]?.[key]
      : undefined

  return (
    <div data-block-type="stat" className="space-y-1 border-y border-border py-4">
      <div className="text-xs font-medium uppercase tracking-normal text-muted-foreground">
        {stringValue(config.label) ?? stringValue(config.title) ?? key ?? "Value"}
      </div>
      {inputs.pending && <div className="text-xs text-muted-foreground">Loading data...</div>}
      {inputs.failed && <div className="text-xs text-destructive">{inputs.failed.state.error ?? "Data failed to load"}</div>}
      <div className="text-3xl font-semibold tabular-nums">{formatValue(selected, stringValue(config.format))}</div>
    </div>
  )
}

function OutputStatus({ state }: { state: OutputState }) {
  if (state.status === "pending") {
    return <div className="text-xs text-muted-foreground">Loading data...</div>
  }
  if (state.status === "error" || state.status === "blocked") {
    return <div className="text-xs text-destructive">{state.error ?? "Data failed to load"}</div>
  }
  return null
}

function EmptyBlock({ label, minHeight }: { label: string; minHeight: number }) {
  return (
    <div className="flex items-center justify-center text-sm text-muted-foreground" style={{ minHeight }}>
      {label}
    </div>
  )
}

function dataBlockPorts(config: Record<string, unknown>): BlockPorts {
  return {
    inputs: [
      { name: "data", type: "rows", required: !isSemanticQuerySpec(config.query) },
      { name: "date_range", type: "date_range", required: false },
    ],
    outputs: [{ name: "data", type: "rows" }],
  }
}

async function fetchBlockRows(
  config: Record<string, unknown>,
  { inputs, ctx, signal }: Pick<EvaluateArgs, "inputs" | "ctx" | "signal">,
): Promise<Row[]> {
  if (Array.isArray(inputs.data)) {
    return inputs.data.filter(isRecord)
  }
  if (isSemanticQuerySpec(config.query)) {
    const range = asDateRange(inputs.date_range)
    const rows = await ctx.runQuery({ ...config.query, date_range: range }, { signal })
    return rows.filter(isRecord)
  }
  throw new Error('Provide a "data" input binding or an inline "query" in config')
}

function semanticQueryPorts(config: Record<string, unknown>): BlockPorts {
  const queries = isRecord(config.queries) ? config.queries : {}
  return {
    inputs: config.compare === true
      ? [
          { name: "date_range", type: "date_range", required: false },
          { name: "compare", type: "compare_ranges", required: true },
        ]
      : [{ name: "date_range", type: "date_range", required: false }],
    outputs: Object.keys(queries).flatMap((name) =>
      config.compare === true
        ? [
            { name, type: "rows" as const },
            { name: `${name}_previous`, type: "rows" as const },
          ]
        : [{ name, type: "rows" as const }],
    ),
  }
}

async function evaluateSemanticQuery({
  config,
  inputs,
  ctx,
  signal,
}: EvaluateArgs): Promise<Record<string, Row[]>> {
  const queries = isRecord(config.queries) ? config.queries : {}
  const compare = asCompare(inputs.compare)
  const dateRange = asDateRange(inputs.date_range)
  const outputs: Record<string, Row[]> = {}

  await Promise.all(
    Object.entries(queries).map(async ([name, query]) => {
      if (!isSemanticQuerySpec(query)) return
      if (config.compare === true && compare) {
        const [current, previous] = await Promise.all([
          ctx.runQuery({ ...query, date_range: compare.current }, { signal }),
          ctx.runQuery({ ...query, date_range: compare.previous }, { signal }),
        ])
        outputs[name] = current
        outputs[`${name}_previous`] = previous
      } else {
        buildSemanticQueryInput({ ...query, date_range: dateRange })
        outputs[name] = await ctx.runQuery({ ...query, date_range: dateRange }, { signal })
      }
    }),
  )

  return outputs
}

function publishPeriodOutputs(engine: { setSourceOutputs: (blockId: string, outputs: Record<string, unknown>) => void }, blockId: string, preset: string) {
  const current = resolvePresetRange(preset)
  const previous = previousPeriod(current)
  const pair: CompareRanges = { current, previous, label: "Previous period" }
  engine.setSourceOutputs(blockId, { current, previous, pair })
}

function periodInitialOutputs(config: Record<string, unknown>) {
  const current = resolvePresetRange(stringValue(config.default_range) ?? "last_30_days")
  const previous = previousPeriod(current)
  const pair: CompareRanges = { current, previous, label: "Previous period" }
  return { current, previous, pair }
}

function rowsFromState(state: OutputState): Row[] {
  return state.value && Array.isArray(state.value) ? state.value.filter(isRecord) : EMPTY_ROWS
}

function inferSeries(config: Record<string, unknown>, rows: Row[], xKey: string) {
  const configured = normalizeGraphSeries(config.series, stringValue(config.y_key), stringValue(config.data_label))
  if (configured.length > 0) return configured
  return Object.keys(rows[0] ?? {})
    .filter((key) => key !== xKey && typeof rows[0]?.[key] === "number")
    .map((key) => ({ data_key: key, label: key }))
}

function tableColumns(config: Record<string, unknown>, rows: Row[]): Array<{ key: string; label: string; format?: string }> {
  const columns = config.columns
  if (Array.isArray(columns) && columns.length > 0) {
    return columns
      .map((item) => {
        if (typeof item === "string") return { key: item, label: item }
        if (isRecord(item)) {
          const key = stringValue(item.key) ?? stringValue(item.accessor)
          return key
            ? { key, label: stringValue(item.label) ?? stringValue(item.header) ?? key, format: stringValue(item.format) }
            : undefined
        }
        return undefined
      })
      .filter((item): item is { key: string; label: string; format?: string } => Boolean(item))
  }
  return Object.keys(rows[0] ?? {}).map((key) => ({ key, label: key }))
}

function collectMissingKeys(tree: RechartsNode, rows: Row[]) {
  const available = new Set(Object.keys(rows[0] ?? {}))
  return collectResultKeyRefs(tree).filter((ref) => !available.has(ref.key))
}

function isRechartsNode(value: unknown): value is RechartsNode {
  return isRecord(value) && typeof value.type === "string"
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

export function buildStoryRegistry(): Map<string, BlockSpec> {
  const specs: BlockSpec[] = [
    {
      type: "title",
      displayName: "Title",
      kind: "visual",
      ports: () => ({ inputs: [], outputs: [] }),
      component: TitleComponent,
    },
    {
      type: "section",
      displayName: "Section",
      kind: "visual",
      ports: () => ({ inputs: [], outputs: [] }),
      component: SectionComponent,
    },
    {
      type: "question",
      displayName: "Question",
      kind: "visual",
      ports: () => ({ inputs: [], outputs: [] }),
      component: QuestionComponent,
    },
    {
      type: "tldr",
      displayName: "TLDR",
      kind: "visual",
      ports: () => ({ inputs: [], outputs: [] }),
      component: TldrComponent,
    },
    {
      type: "markdown",
      displayName: "Markdown",
      kind: "visual",
      ports: () => ({ inputs: [], outputs: [] }),
      component: MarkdownComponent,
    },
    {
      type: "date_filter",
      displayName: "Date Filter",
      kind: "source",
      ports: () => ({ inputs: [], outputs: [{ name: "value", type: "date_range" }] }),
      initialOutputs: (config) => ({ value: resolvePresetRange(stringValue(config.default)) }),
      component: DateFilterComponent,
    },
    {
      type: "period_selector",
      displayName: "Period Selector",
      kind: "source",
      ports: () => ({
        inputs: [],
        outputs: [
          { name: "current", type: "date_range" },
          { name: "previous", type: "date_range" },
          { name: "pair", type: "compare_ranges" },
        ],
      }),
      initialOutputs: periodInitialOutputs,
      component: PeriodSelectorComponent,
    },
    {
      type: "semantic_query",
      displayName: "Semantic Query",
      kind: "compute",
      hiddenByDefault: true,
      ports: semanticQueryPorts,
      evaluate: evaluateSemanticQuery,
    },
    {
      type: "graph",
      displayName: "Graph",
      kind: "visual",
      ports: dataBlockPorts,
      evaluate: async ({ config, inputs, ctx, signal }) => ({
        data: await fetchBlockRows(config, { inputs, ctx, signal }),
      }),
      component: GraphComponent,
    },
    {
      type: "table",
      displayName: "Table",
      kind: "visual",
      ports: dataBlockPorts,
      evaluate: async ({ config, inputs, ctx, signal }) => ({
        data: await fetchBlockRows(config, { inputs, ctx, signal }),
      }),
      component: TableComponent,
    },
    {
      type: "stat",
      displayName: "Stat",
      kind: "visual",
      ports: () => ({
        inputs: [
          { name: "current", type: "rows", required: true },
          { name: "previous", type: "rows", required: false },
        ],
        outputs: [],
      }),
      component: StatComponent,
    },
  ]
  return new Map(specs.map((spec) => [spec.type, spec]))
}

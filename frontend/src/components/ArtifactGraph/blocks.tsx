/* eslint-disable react-refresh/only-export-components */

import {
  ArrowDownRight,
  ArrowUpRight,
  CalendarDays,
  CalendarRange,
  MessageCircleQuestion,
  Minus,
  ScanText,
} from "lucide-react"
import { useId } from "react"
import type React from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ResponsiveContainer } from "recharts"

import { cn } from "@/lib/utils"

import { firstNumericKey, formatValue, numeric, pathKey, selectPath } from "./format"
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
  COMPARISON_LABELS,
  comparisonPeriod,
  isRecord,
  normalizeComparisonPreset,
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
  description,
  children,
  className,
}: {
  title?: string
  description?: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <section className={cn("space-y-3", className)}>
      {(title || description) && (
        <header className="space-y-1">
          {title && <h3 className="text-sm font-semibold">{title}</h3>}
          {description && <p className="text-xs text-muted-foreground">{description}</p>}
        </header>
      )}
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

function SectionComponent({ block, config }: BlockComponentProps) {
  const generatedId = useId()
  const titleId = `artifact-section-${block.id.replace(/[^a-zA-Z0-9_-]/g, "-")}-${generatedId.replace(/:/g, "")}`
  const title = stringValue(config.title)
  const body = stringValue(config.body) ?? stringValue(config.text) ?? ""
  if (!title && !body) return null

  return (
    <section
      aria-labelledby={title ? titleId : undefined}
      className="grid gap-3 border-t border-border pt-5 sm:grid-cols-[11rem_minmax(0,1fr)] sm:gap-6"
      data-block-type="section"
    >
      {title && (
        <h2 id={titleId} className="text-base font-semibold leading-6 tracking-[-0.015em]">
          {title}
        </h2>
      )}
      {body && (
        <div className="min-w-0 max-w-[70ch] text-muted-foreground">
          <MarkdownBlockContent content={body} />
        </div>
      )}
    </section>
  )
}

function QuestionComponent({ block, config }: BlockComponentProps) {
  const question = stringValue(config.text) ?? stringValue(config.question)
  const context = stringValue(config.context)
  const generatedId = useId()
  const titleId = `artifact-question-${block.id.replace(/[^a-zA-Z0-9_-]/g, "-")}-${generatedId.replace(/:/g, "")}`
  if (!question && !context) return null

  return (
    <section
      aria-labelledby={question ? titleId : undefined}
      className="flex gap-4 rounded-xl bg-muted/60 px-5 py-4 text-foreground sm:px-6 sm:py-5"
      data-block-type="question"
    >
      <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-background text-muted-foreground shadow-sm">
        <MessageCircleQuestion aria-hidden="true" className="h-4.5 w-4.5" />
      </span>
      <div className="min-w-0 space-y-1.5 pt-1">
        {question && (
          <h2 id={titleId} className="max-w-[65ch] text-base font-semibold leading-6 tracking-[-0.015em] sm:text-lg">
            {question}
          </h2>
        )}
        {context && <p className="max-w-[70ch] text-sm leading-6 text-muted-foreground">{context}</p>}
      </div>
    </section>
  )
}

function TldrComponent({ block, config }: BlockComponentProps) {
  const items = Array.isArray(config.items) ? config.items : []
  const content = stringValue(config.content)
  const generatedId = useId()
  const titleId = `artifact-summary-${block.id.replace(/[^a-zA-Z0-9_-]/g, "-")}-${generatedId.replace(/:/g, "")}`
  const title = stringValue(config.title) ?? "In brief"

  if (items.length === 0 && !content) return null

  return (
    <section
      aria-labelledby={titleId}
      className="overflow-hidden rounded-xl bg-foreground text-background"
      data-block-type="tldr"
    >
      <div className="grid sm:grid-cols-[11rem_minmax(0,1fr)]">
        <header className="flex items-center gap-3 border-b border-background/15 px-5 py-4 sm:items-start sm:border-r sm:border-b-0 sm:py-5">
          <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-background/15 bg-background/10">
            <ScanText aria-hidden="true" className="h-4 w-4 text-background/75" />
          </span>
          <h2 id={titleId} className="pt-1 text-sm font-semibold tracking-[-0.01em]">
            {title}
          </h2>
        </header>
        {items.length > 0 ? (
          <ul className="min-w-0 divide-y divide-background/15 px-5 sm:px-6">
            {items.map((item, index) => (
              <li key={index} className="max-w-[70ch] break-words py-3.5 text-sm font-medium leading-6 text-background/90">
                {String(item)}
              </li>
            ))}
          </ul>
        ) : (
          <p className="min-w-0 max-w-[70ch] break-words px-5 py-5 text-sm font-medium leading-6 text-background/90 sm:px-6">
            {content}
          </p>
        )}
      </div>
    </section>
  )
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
    <div data-block-type="date_filter" className="rounded-xl border border-border bg-card p-4 text-card-foreground">
      <div className="grid gap-4">
        <label className="grid gap-2 text-sm font-medium">
          <span className="inline-flex items-center gap-2">
            <CalendarDays aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
            {stringValue(config.label) ?? "Date range"}
          </span>
          <select
            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground shadow-xs outline-none transition-[border-color,box-shadow] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
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
        <div className="grid gap-3 border-t border-border pt-3 sm:grid-cols-2">
          <DateInput
            label="Start date"
            value={value.start}
            onChange={(start) => engine.setSourceOutputs(block.id, { value: { ...value, start, preset: "custom" } })}
          />
          <DateInput
            label="End date"
            value={value.end}
            onChange={(end) => engine.setSourceOutputs(block.id, { value: { ...value, end, preset: "custom" } })}
          />
        </div>
      </div>
    </div>
  )
}

function PeriodSelectorComponent({ block, config, engine }: BlockComponentProps) {
  const currentState = useOutput(engine, outputKey(block.id, "current"))
  const previousState = useOutput(engine, outputKey(block.id, "previous"))
  const value = asDateRange(currentState.value) ?? resolvePresetRange(stringValue(config.default_range) ?? "last_30_days")
  const comparison = normalizeComparisonPreset(config.default_comparison)
  const previous = asDateRange(previousState.value) ?? comparisonPeriod(value, comparison)
  const comparisonLabel = COMPARISON_LABELS[comparison]

  return (
    <div data-block-type="period_selector" className="rounded-xl border border-border bg-card p-4 text-card-foreground">
      <div className="grid gap-4">
        <label className="grid gap-2 text-sm font-medium">
          <span className="inline-flex items-center gap-2">
            <CalendarRange aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
            {stringValue(config.label) ?? "Comparison period"}
          </span>
          <select
            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground shadow-xs outline-none transition-[border-color,box-shadow] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
            value={value.preset ?? "last_30_days"}
            onChange={(event) => publishPeriodOutputs(engine, block.id, event.target.value, comparison)}
          >
            <option value="last_7_days">Last 7 days</option>
            <option value="last_30_days">Last 30 days</option>
            <option value="last_90_days">Last 90 days</option>
            <option value="month_to_date">Month to date</option>
          </select>
        </label>
        <dl className="grid gap-3 border-t border-border pt-3 sm:grid-cols-2">
          <PeriodSummary label="Selected period" range={value} />
          <PeriodSummary label={comparisonLabel} range={previous} />
        </dl>
      </div>
    </div>
  )
}

function PeriodSummary({ label, range }: { label: string; range: DateRange }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-sm font-medium tabular-nums">{formatDateRange(range)}</dd>
    </div>
  )
}

function formatDateRange(range: DateRange): string {
  const start = localDate(range.start)
  const end = localDate(range.end)
  if (!start || !end) return `${range.start} – ${range.end}`

  const shortDate = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" })
  const fullDate = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" })
  if (start.getFullYear() === end.getFullYear()) {
    return `${shortDate.format(start)} – ${shortDate.format(end)}, ${end.getFullYear()}`
  }
  return `${fullDate.format(start)} – ${fullDate.format(end)}`
}

function localDate(value: string): Date | null {
  const parts = value.split("-").map((part) => Number.parseInt(part, 10))
  if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return null
  const [year, month, day] = parts
  const date = new Date(year, month - 1, day)
  return Number.isNaN(date.getTime()) ? null : date
}

function DateInput({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="grid min-w-0 gap-1.5 text-xs font-medium text-muted-foreground">
      {label}
      <input
        type="date"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-9 min-w-0 w-full rounded-md border border-input bg-background px-2 text-sm tabular-nums text-foreground shadow-xs outline-none transition-[border-color,box-shadow] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
      />
    </label>
  )
}

function GraphComponent({ block, config, engine }: BlockComponentProps) {
  const state = useOutput(engine, outputKey(block.id, "data"))
  const rows = rowsFromState(state)
  const xKey = stringValue(config.x_key) ?? "date"
  const inferredSeries = inferSeries(config, rows, xKey)
  const style = isRecord(config.style) ? config.style : {}
  const chartConfig = {
    chart_type: stringValue(config.chart_type) ?? "line",
    x_key: xKey,
    y_key: stringValue(config.y_key),
    series: inferredSeries,
    data_label: stringValue(config.data_label),
    y_format: stringValue(config.y_format),
    stacked: config.stacked === true,
    palette: stringValue(style.palette),
    legend: stringValue(style.legend),
    grid: stringValue(style.grid),
    labels: stringValue(style.labels),
    curve: stringValue(style.curve),
    orientation: stringValue(style.orientation),
    x_label: stringValue(config.x_label),
    y_label: stringValue(config.y_label),
  }
  const height = typeof config.height === "number" && Number.isFinite(config.height) ? config.height : 280
  let tree: RechartsNode
  try {
    tree = isRechartsNode(config.recharts) ? config.recharts : compileCompactGraphConfig(chartConfig)
  } catch (error) {
    return (
      <BlockCard title={stringValue(config.title)} description={stringValue(config.subtitle)}>
        <div className="flex min-h-48 items-center justify-center rounded-md border border-destructive/30 px-4 text-sm text-destructive">
          Chart config error: {error instanceof Error ? error.message : String(error)}
        </div>
      </BlockCard>
    )
  }
  const missing = rows.length > 0 ? collectMissingKeys(tree, rows) : []

  return (
    <BlockCard title={stringValue(config.title)} description={stringValue(config.subtitle)}>
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
  let chart: React.ReactNode
  try {
    chart = buildRechartsTree(tree, rows)
  } catch (error) {
    return (
      <div className="flex min-h-48 items-center justify-center rounded-md border border-destructive/30 px-4 text-sm text-destructive">
        Chart config error: {error instanceof Error ? error.message : String(error)}
      </div>
    )
  }

  return (
    <div data-block-type="graph" style={{ width: "100%", height }}>
      <ResponsiveContainer width="100%" height="100%">
        {chart}
      </ResponsiveContainer>
    </div>
  )
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
  const valueKey = stringValue(config.value_key)
  const valuePath = stringValue(config.value_path)
  const key = valueKey ?? pathKey(valuePath) ?? firstNumericKey(rows[0])
  const selected = selectStatValue(inputs.values.current, valueKey, valuePath, key)
  const previousSelected = selectStatValue(inputs.values.previous, valueKey, valuePath, key)
  const deltaPath = stringValue(config.delta_path)
  const explicitDelta = deltaPath ? numeric(selectPath(inputs.values.current, deltaPath)) : null
  const selectedNumber = numeric(selected)
  const previousNumber = numeric(previousSelected)
  const delta = explicitDelta ?? (
    selectedNumber !== null && previousNumber !== null ? selectedNumber - previousNumber : null
  )
  const format = stringValue(config.format)
  const prefix = stringValue(config.prefix) ?? ""
  const suffix = stringValue(config.suffix) ?? ""
  const comparison = isRecord(config.comparison) ? config.comparison : {}
  const comparisonType = comparison.type === "none" ? "none" : comparison.type === "percent" ? "percent" : "absolute"
  const comparisonDelta = comparisonType === "none"
    ? null
    : comparisonType === "percent"
      ? previousNumber !== null && previousNumber !== 0 && delta !== null
        ? delta / Math.abs(previousNumber)
        : null
      : delta
  const comparisonFormat = stringValue(comparison.format) ?? (comparisonType === "percent" ? "percent_1" : format)

  return (
    <div
      data-block-type="stat"
      className="flex min-h-40 flex-col rounded-xl border border-border bg-card p-4 text-card-foreground"
    >
      <div className="text-sm font-medium text-muted-foreground">
        {stringValue(config.label) ?? stringValue(config.title) ?? key ?? "Value"}
      </div>
      {inputs.pending && <div className="mt-2 text-xs text-muted-foreground">Loading metric…</div>}
      {inputs.failed && <div className="mt-2 text-xs text-destructive">{inputs.failed.state.error ?? "Data failed to load"}</div>}
      <div className="mt-3 text-4xl font-semibold leading-none tracking-[-0.025em] tabular-nums">
        {formatWithAffixes(selected, format, prefix, suffix)}
      </div>
      {comparisonDelta !== null && (
        <StatDelta
          delta={comparisonDelta}
          rawDelta={delta ?? comparisonDelta}
          previous={previousNumber}
          format={comparisonFormat}
          previousFormat={format}
          comparisonLabel={stringValue(comparison.label)}
          trendDirection={normalizeTrendDirection(comparison.goal)}
          prefix={comparisonType === "absolute" ? prefix : ""}
          suffix={comparisonType === "absolute" ? suffix : ""}
          previousPrefix={prefix}
          previousSuffix={suffix}
        />
      )}
    </div>
  )
}

function selectStatValue(
  input: unknown,
  valueKey: string | undefined,
  valuePath: string | undefined,
  fallbackKey: string | undefined,
): unknown {
  const rows = Array.isArray(input) ? input.filter(isRecord) : EMPTY_ROWS
  if (valueKey) return rows[0]?.[valueKey]
  if (valuePath) return selectPath(input, valuePath)
  return fallbackKey ? rows[0]?.[fallbackKey] : undefined
}

type TrendDirection = "higher" | "lower" | "neutral"

function StatDelta({
  delta,
  rawDelta,
  previous,
  format,
  previousFormat,
  comparisonLabel,
  trendDirection,
  prefix,
  suffix,
  previousPrefix,
  previousSuffix,
}: {
  delta: number
  rawDelta: number
  previous: number | null
  format?: string
  previousFormat?: string
  comparisonLabel?: string
  trendDirection: TrendDirection
  prefix: string
  suffix: string
  previousPrefix: string
  previousSuffix: string
}) {
  const absoluteDelta = formatWithAffixes(Math.abs(delta), format, prefix, suffix)
  const previousValue = previous === null
    ? null
    : formatWithAffixes(previous, previousFormat, previousPrefix, previousSuffix)
  const label = rawDelta > 0
    ? `Increased by ${absoluteDelta}`
    : rawDelta < 0
      ? `Decreased by ${absoluteDelta}`
      : "No change"
  const Icon = rawDelta > 0 ? ArrowUpRight : rawDelta < 0 ? ArrowDownRight : Minus
  const sentiment = statSentiment(rawDelta, trendDirection)
  const outcome = sentiment === "positive" ? "Favorable" : sentiment === "negative" ? "Unfavorable" : "Neutral"

  return (
    <div
      aria-label={`${label}${previousValue === null ? "" : ` from previous value ${previousValue}`}; ${outcome.toLowerCase()} outcome${comparisonLabel ? `; ${comparisonLabel}` : ""}`}
      className="mt-auto flex items-center gap-2 border-t border-border pt-3 text-xs text-muted-foreground"
      data-stat-delta
    >
      <span
        className={cn(
          "inline-flex h-6 shrink-0 items-center gap-1 rounded-md px-2 font-semibold",
          sentiment === "positive" && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
          sentiment === "negative" && "bg-destructive/10 text-destructive",
          sentiment === "neutral" && "bg-primary/10 text-primary",
        )}
      >
        <Icon aria-hidden="true" className="h-3.5 w-3.5" />
        <span className="tabular-nums">{rawDelta > 0 ? "+" : rawDelta < 0 ? "−" : ""}{absoluteDelta}</span>
      </span>
      <span className="font-medium text-foreground">{outcome}</span>
      <span aria-hidden="true" data-stat-period className="min-w-0 truncate">
        · {comparisonLabel ?? (previousValue === null ? "change" : `from ${previousValue}`)}
      </span>
    </div>
  )
}

function normalizeTrendDirection(value: unknown): TrendDirection {
  if (value === "higher" || value === "lower") return value
  return "neutral"
}

function statSentiment(delta: number, direction: TrendDirection): "positive" | "negative" | "neutral" {
  if (direction === "neutral" || delta === 0) return "neutral"
  const favorable = direction === "higher" ? delta > 0 : delta < 0
  return favorable ? "positive" : "negative"
}

function formatWithAffixes(value: unknown, format: string | undefined, prefix: string, suffix: string): string {
  return `${prefix}${formatValue(value, format)}${suffix}`
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

function publishPeriodOutputs(
  engine: { setSourceOutputs: (blockId: string, outputs: Record<string, unknown>) => void },
  blockId: string,
  preset: string,
  comparison: ReturnType<typeof normalizeComparisonPreset>,
) {
  const current = resolvePresetRange(preset)
  const previous = comparisonPeriod(current, comparison)
  const pair: CompareRanges = { current, previous, label: COMPARISON_LABELS[comparison] }
  engine.setSourceOutputs(blockId, { current, previous, pair })
}

function periodInitialOutputs(config: Record<string, unknown>) {
  const current = resolvePresetRange(stringValue(config.default_range) ?? "last_30_days")
  const comparison = normalizeComparisonPreset(config.default_comparison)
  const previous = comparisonPeriod(current, comparison)
  const pair: CompareRanges = { current, previous, label: COMPARISON_LABELS[comparison] }
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

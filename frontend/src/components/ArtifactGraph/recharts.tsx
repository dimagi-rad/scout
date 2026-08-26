/* eslint-disable react-refresh/only-export-components */

import React from "react"
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"

import { formatValue } from "./format"
import type { Row } from "./types"

export interface RechartsNode {
  type: string
  props?: Record<string, unknown>
  children?: RechartsNode[]
  palette?: string[]
}

export interface GraphSeries {
  data_key: string
  label?: string
  color?: string
}

export const CHART_PALETTES = {
  categorical: ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"],
  status: ["var(--chart-1)", "var(--chart-warning)", "var(--destructive)", "var(--chart-positive)", "var(--muted-foreground)"],
  sequential: [
    "var(--chart-1)",
    "color-mix(in oklch, var(--chart-1) 78%, var(--background))",
    "color-mix(in oklch, var(--chart-1) 58%, var(--background))",
    "color-mix(in oklch, var(--chart-1) 38%, var(--background))",
    "color-mix(in oklch, var(--chart-1) 22%, var(--background))",
  ],
  monochrome: [
    "var(--foreground)",
    "color-mix(in oklch, var(--foreground) 78%, var(--background))",
    "color-mix(in oklch, var(--foreground) 58%, var(--background))",
    "color-mix(in oklch, var(--foreground) 38%, var(--background))",
    "color-mix(in oklch, var(--foreground) 22%, var(--background))",
  ],
} as const

export const SERIES_COLORS = [...CHART_PALETTES.categorical]
const SAFE_CHART_COLORS = new Set<string>(Object.values(CHART_PALETTES).flat().map(String))

const RECHARTS_REGISTRY: Record<string, React.ElementType> = {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceLine,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
}

const CHART_TYPES = new Set(["AreaChart", "BarChart", "ComposedChart", "LineChart", "PieChart", "ScatterChart"])
const SERIES_TYPES = new Set(["Area", "Bar", "Line", "Pie", "Scatter"])
const DATA_INJECT_TYPES = new Set(["AreaChart", "BarChart", "ComposedChart", "LineChart", "Pie", "PieChart", "ScatterChart"])
const RESULT_KEY_PROPS = new Set(["dataKey", "nameKey", "xAxisKey", "yAxisKey"])

const RECHARTS_PROP_ALLOWLIST: Record<string, ReadonlySet<string>> = {
  AreaChart: new Set(["layout", "margin", "stackOffset", "syncId"]),
  BarChart: new Set(["layout", "margin", "stackOffset", "barCategoryGap", "barGap", "syncId"]),
  ComposedChart: new Set(["layout", "margin", "stackOffset", "barCategoryGap", "barGap", "syncId"]),
  LineChart: new Set(["layout", "margin", "syncId"]),
  PieChart: new Set(["margin"]),
  ScatterChart: new Set(["layout", "margin", "syncId"]),
  CartesianGrid: new Set(["horizontal", "vertical", "stroke", "strokeDasharray"]),
  XAxis: new Set([
    "allowDecimals", "angle", "axisLine", "dataKey", "domain", "height", "hide", "interval", "label",
    "minTickGap", "name", "orientation", "padding", "reversed", "scale", "tickCount",
    "tickFormatter", "tickLine", "tickMargin", "ticks", "type", "unit", "width", "xAxisId",
  ]),
  YAxis: new Set([
    "allowDecimals", "angle", "axisLine", "dataKey", "domain", "height", "hide", "interval", "label",
    "minTickGap", "name", "orientation", "padding", "reversed", "scale", "tickCount",
    "tickFormatter", "tickLine", "tickMargin", "ticks", "type", "unit", "width", "yAxisId",
  ]),
  Tooltip: new Set(["cursor", "formatter", "labelFormatter", "separator"]),
  Legend: new Set(["align", "iconSize", "iconType", "layout", "verticalAlign"]),
  Line: new Set([
    "activeDot", "connectNulls", "dataKey", "dot", "hide", "name", "stroke", "strokeWidth",
    "label", "type", "unit", "xAxisId", "yAxisId",
  ]),
  Area: new Set([
    "activeDot", "connectNulls", "dataKey", "dot", "fill", "fillOpacity", "hide", "name",
    "label", "stackId", "stroke", "strokeWidth", "type", "unit", "xAxisId", "yAxisId",
  ]),
  Bar: new Set([
    "barSize", "dataKey", "fill", "hide", "label", "maxBarSize", "name", "radius", "stackId",
    "unit", "xAxisId", "yAxisId",
  ]),
  Pie: new Set([
    "cx", "cy", "dataKey", "endAngle", "innerRadius", "label", "labelLine", "minAngle", "nameKey",
    "outerRadius", "paddingAngle", "startAngle",
  ]),
  Scatter: new Set(["fill", "hide", "line", "lineType", "name", "shape", "xAxisId", "yAxisId"]),
  ReferenceLine: new Set(["ifOverflow", "label", "stroke", "strokeDasharray", "x", "xAxisId", "y", "yAxisId"]),
  Cell: new Set(["fill", "stroke"]),
}

const COLOR_PROPS = new Set(["fill", "stroke"])

export interface ResultKeyRef {
  key: string
  where: string
}

export function RechartsFrame({
  rows,
  tree,
  height,
}: {
  rows: Row[]
  tree: RechartsNode
  height: number
}) {
  return (
    <div data-block-type="graph" style={{ width: "100%", height }}>
      <ResponsiveContainer width="100%" height="100%">
        {buildRechartsTree(tree, rows)}
      </ResponsiveContainer>
    </div>
  )
}

export function collectResultKeyRefs(node: RechartsNode, path?: string): ResultKeyRef[] {
  const where = path ?? node?.type ?? "recharts"
  const refs: ResultKeyRef[] = []
  for (const [name, value] of Object.entries(node.props ?? {})) {
    if (RESULT_KEY_PROPS.has(name) && typeof value === "string") {
      refs.push({ key: value, where: `${where} ${name}` })
    }
  }
  for (const child of node.children ?? []) {
    refs.push(...collectResultKeyRefs(child, `${where}.${child.type}`))
  }
  return refs
}

export function buildRechartsTree(tree: RechartsNode, rows: Row[]): React.ReactElement {
  if (!CHART_TYPES.has(tree?.type)) {
    throw new Error(`Recharts root must be one of ${[...CHART_TYPES].join(", ")}`)
  }
  const normalizedRows = normalizeNumericSeriesRows(tree, rows)
  return buildNode(tree, { rows: normalizedRows, seriesIndex: 0, palette: validPalette(tree.palette) })
}

export interface CompactGraphConfig {
  chart_type?: string
  x_key?: string
  y_key?: string
  series?: unknown
  data_label?: string
  y_format?: string
  stacked?: boolean
  palette?: string
  legend?: string
  grid?: string
  labels?: string
  curve?: string
  orientation?: string
  x_label?: string
  y_label?: string
}

export function compileCompactGraphConfig(config: CompactGraphConfig): RechartsNode {
  const chartType = config.chart_type ?? "line"
  if (!["line", "bar", "area", "pie", "donut"].includes(chartType)) {
    throw new Error(`Unsupported compact chart type "${chartType}"`)
  }
  const xKey = config.x_key ?? "date"
  const series = normalizeGraphSeries(config.series, config.y_key, config.data_label)
  const yFormatName = config.y_format ?? inferSeriesFormat(series)
  const hasCountSemantic = config.y_format === undefined && yFormatName === "number_0"
  const yFormat = (value: unknown) => formatValue(value, yFormatName)
  const palette = namedPalette(config.palette)
  const colorOf = (index: number) => series[index]?.color ?? palette[index % palette.length]
  const showLegend = config.legend !== "none" && (
    config.legend === "top" || config.legend === "bottom" || series.length > 1 || ["pie", "donut"].includes(chartType)
  )
  const legend = showLegend
    ? [{ type: "Legend", props: { verticalAlign: config.legend === "top" ? "top" : "bottom" } }]
    : []

  if (chartType === "pie" || chartType === "donut") {
    return {
      type: "PieChart",
      palette,
      children: [
        { type: "Tooltip", props: { formatter: yFormat } },
        {
          type: "Pie",
          props: {
            dataKey: series[0]?.data_key ?? "value",
            nameKey: xKey,
            innerRadius: chartType === "donut" ? "52%" : 0,
            outerRadius: "82%",
            paddingAngle: 2,
            label: config.labels === "value"
              ? (entry: { value?: unknown }) => yFormat(entry.value)
              : false,
          },
        },
        ...legend,
      ],
    }
  }

  const horizontalBars = config.chart_type === "bar" && config.orientation === "horizontal"
  const curve = ["linear", "monotone", "step"].includes(config.curve ?? "") ? config.curve : "monotone"
  const chartMargin = {
    top: showLegend && config.legend === "top" ? 12 : 8,
    right: config.labels === "value" ? 44 : 12,
    bottom: config.x_label ? 24 : 8,
    left: config.y_label ? 12 : 0,
  }
  const axes: RechartsNode[] = [
    ...(config.grid === "none"
      ? []
      : [{ type: "CartesianGrid", props: { vertical: config.grid === "both", horizontal: true } }]),
    horizontalBars
        ? {
          type: "XAxis",
          props: {
            type: "number",
            allowDecimals: yFormatName !== "number_0",
            domain: hasCountSemantic ? [0, "dataMax"] : undefined,
            tickFormatter: yFormat,
            label: config.y_label ? axisLabel(config.y_label, "insideBottom") : undefined,
          },
        }
      : {
          type: "XAxis",
          props: {
            dataKey: xKey,
            label: config.x_label ? axisLabel(config.x_label, "insideBottom") : undefined,
          },
        },
    horizontalBars
      ? { type: "YAxis", props: { type: "category", dataKey: xKey, width: 88 } }
      : {
          type: "YAxis",
          props: {
            allowDecimals: yFormatName !== "number_0",
            domain: hasCountSemantic ? [0, "dataMax"] : undefined,
            tickFormatter: yFormat,
            label: config.y_label ? { ...axisLabel(config.y_label, "insideLeft"), angle: -90 } : undefined,
          },
        },
    { type: "Tooltip", props: { formatter: yFormat } },
    ...legend,
  ]

  if (config.chart_type === "bar") {
    return {
      type: "BarChart",
      palette,
      props: { layout: horizontalBars ? "vertical" : "horizontal", margin: chartMargin },
      children: [
        ...axes,
        ...series.map((entry, index) => ({
          type: "Bar",
          props: {
            dataKey: entry.data_key,
            name: entry.label ?? entry.data_key,
            fill: colorOf(index),
            stackId: config.stacked ? "stack" : undefined,
            radius: horizontalBars ? [0, 6, 6, 0] : [6, 6, 0, 0],
            label: config.labels === "value"
              ? { position: horizontalBars ? "right" : "top", formatter: yFormat, fontSize: 11 }
              : undefined,
          },
        })),
      ],
    }
  }

  if (config.chart_type === "area") {
    return {
      type: "AreaChart",
      palette,
      props: { margin: chartMargin },
      children: [
        ...axes,
        ...series.map((entry, index) => ({
          type: "Area",
          props: {
            type: curve,
            dataKey: entry.data_key,
            name: entry.label ?? entry.data_key,
            stroke: colorOf(index),
            fill: colorOf(index),
            stackId: config.stacked ? "stack" : undefined,
            label: config.labels === "value"
              ? { position: "top", formatter: yFormat, fontSize: 11 }
              : undefined,
          },
        })),
      ],
    }
  }

  return {
    type: "LineChart",
    palette,
    props: { margin: chartMargin },
    children: [
      ...axes,
      ...series.map((entry, index) => ({
        type: "Line",
        props: {
          type: curve,
          dataKey: entry.data_key,
          name: entry.label ?? entry.data_key,
          stroke: colorOf(index),
          label: config.labels === "value"
            ? { position: "top", formatter: yFormat, fontSize: 11 }
            : undefined,
        },
      })),
    ],
  }
}

export function normalizeGraphSeries(series: unknown, yKey?: string, dataLabel?: string): GraphSeries[] {
  if (Array.isArray(series) && series.length > 0) {
    return series
      .map((item): GraphSeries | undefined => {
        if (typeof item === "string") return { data_key: item, label: item }
        if (isRecord(item)) {
          const dataKey = stringValue(item.data_key) ?? stringValue(item.y_key) ?? stringValue(item.key)
          return dataKey
            ? {
                data_key: dataKey,
                label: stringValue(item.label) ?? stringValue(item.name) ?? dataKey,
                color: safeColor(stringValue(item.color)),
              }
            : undefined
        }
        return undefined
      })
      .filter(isGraphSeries)
  }
  if (yKey) return [{ data_key: yKey, label: dataLabel ?? yKey }]
  return []
}

function buildNode(
  node: RechartsNode,
  state: { rows: Row[]; seriesIndex: number; palette: string[] },
  key?: React.Key,
): React.ReactElement {
  const component = RECHARTS_REGISTRY[node.type]
  if (!component) {
    throw new Error(`Unknown Recharts component "${node.type}"`)
  }

  const seriesIndex = state.seriesIndex
  if (SERIES_TYPES.has(node.type)) {
    state.seriesIndex += 1
  }

  const props: Record<string, unknown> = {}
  for (const [name, value] of Object.entries(safeNodeProps(node.type, node.props ?? {}))) {
    props[name] = resolveProp(value)
  }
  const defaulted = applyDefaults(node.type, props, seriesIndex, state.palette)
  if (DATA_INJECT_TYPES.has(node.type)) {
    defaulted.data = resolveDataProp(node.type, defaulted.data, state.rows)
  }
  if (key !== undefined) {
    defaulted.key = key
  }

  let children = node.children?.map((child, index) => buildNode(child, state, index))
  if (node.type === "Pie" && !node.children?.some((child) => child.type === "Cell")) {
    children = [
      ...(children ?? []),
      ...state.rows.map((_, index) => (
        <Cell key={`auto-cell-${index}`} fill={state.palette[index % state.palette.length]} />
      )),
    ]
  }

  return React.createElement(component, defaulted, ...(children ?? []))
}

function applyDefaults(
  type: string,
  props: Record<string, unknown>,
  seriesIndex: number,
  palette: string[],
): Record<string, unknown> {
  const out = { ...props }
  const defaultColor = palette[seriesIndex % palette.length]
  if (type === "CartesianGrid") {
    out.stroke = out.stroke ?? "var(--border)"
    out.strokeDasharray = out.strokeDasharray ?? "4 6"
  }
  if (type === "XAxis" || type === "YAxis") {
    out.tick = out.tick ?? { fontSize: 11, fill: "var(--muted-foreground)" }
    out.tickLine = out.tickLine ?? false
    out.axisLine = out.axisLine ?? false
  }
  if (type === "XAxis") {
    out.tickFormatter = out.tickFormatter ?? formatAxisTick
  }
  if (type === "YAxis") {
    out.width = out.width ?? 56
  }
  if (type === "Tooltip") {
    out.content = out.content ?? React.createElement(AccessibleTooltipContent, {
      valueFormatter: out.formatter,
      labelFormatter: out.labelFormatter,
    })
    out.contentStyle = out.contentStyle ?? {
      backgroundColor: "var(--popover)",
      color: "var(--popover-foreground)",
      borderRadius: 10,
      borderColor: "var(--border)",
      boxShadow: "0 10px 28px rgb(15 23 42 / 0.14)",
      padding: "10px 12px",
    }
  }
  if (type === "Legend") {
    out.iconType = out.iconType ?? "circle"
    out.iconSize = out.iconSize ?? 8
    out.formatter = out.formatter ?? ((value: unknown) => (
      <span style={{ color: "var(--foreground)" }}>{String(value)}</span>
    ))
    out.wrapperStyle = out.wrapperStyle ?? { fontSize: 12 }
  }
  if (type === "Line") {
    out.stroke = out.stroke ?? defaultColor
    out.strokeWidth = out.strokeWidth ?? 2.5
    out.dot = out.dot ?? false
  }
  if (type === "Area") {
    out.stroke = out.stroke ?? defaultColor
    out.fill = out.fill ?? defaultColor
    out.fillOpacity = out.fillOpacity ?? 0.16
  }
  if (type === "Bar") {
    out.fill = out.fill ?? defaultColor
    out.radius = out.radius ?? [6, 6, 0, 0]
  }
  if (type === "Scatter") {
    out.fill = out.fill ?? defaultColor
  }
  return out
}

function axisLabel(value: string, position: string) {
  return { value, position, fill: "var(--muted-foreground)", fontSize: 11 }
}

function inferSeriesFormat(series: GraphSeries[]): string {
  if (series.length > 0 && series.every((entry) => /(^|_)count$/.test(entry.data_key))) {
    return "number_0"
  }
  return "compact"
}

function namedPalette(name: string | undefined): string[] {
  return name && name in CHART_PALETTES
    ? [...CHART_PALETTES[name as keyof typeof CHART_PALETTES]]
    : [...CHART_PALETTES.categorical]
}

function validPalette(value: unknown): string[] {
  if (value === undefined) return [...SERIES_COLORS]
  if (Array.isArray(value) && value.length > 0 && value.every((color) => safeColor(color) !== undefined)) {
    return value as string[]
  }
  throw new Error("Recharts palette may contain only Scout chart color tokens")
}

function safeNodeProps(type: string, props: Record<string, unknown>): Record<string, unknown> {
  const allowed = RECHARTS_PROP_ALLOWLIST[type] ?? new Set<string>()
  const safe: Record<string, unknown> = {}
  for (const [name, value] of Object.entries(props)) {
    if (!allowed.has(name)) {
      throw new Error(`Recharts ${type} prop "${name}" is not supported`)
    }
    if (COLOR_PROPS.has(name) && !isSafeComponentColor(type, value)) {
      throw new Error(`Recharts ${type} prop "${name}" must use a Scout chart color token`)
    }
    safe[name] = value
  }
  return safe
}

function isSafeComponentColor(type: string, value: unknown): boolean {
  return safeColor(value) !== undefined
    || ((type === "CartesianGrid" || type === "ReferenceLine") && value === "var(--border)")
}

function safeColor(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined
  return SAFE_CHART_COLORS.has(value) ? value : undefined
}

function normalizeNumericSeriesRows(tree: RechartsNode, rows: Row[]): Row[] {
  const numericKeys = collectNumericDataKeys(tree)
  if (numericKeys.size === 0) return rows
  return rows.map((row) => {
    const normalized = { ...row }
    for (const key of numericKeys) {
      const value = normalized[key]
      if (typeof value === "string" && value.trim() !== "") {
        const number = Number(value)
        if (Number.isFinite(number)) normalized[key] = number
      }
    }
    return normalized
  })
}

function collectNumericDataKeys(node: RechartsNode, keys = new Set<string>()): Set<string> {
  const dataKey = node.props?.dataKey
  if (
    typeof dataKey === "string"
    && (SERIES_TYPES.has(node.type) || ((node.type === "XAxis" || node.type === "YAxis") && node.props?.type === "number"))
  ) {
    keys.add(dataKey)
  }
  for (const child of node.children ?? []) collectNumericDataKeys(child, keys)
  return keys
}

function resolveProp(value: unknown): unknown {
  if (isRecord(value) && typeof value.$format === "string") {
    const format = value.$format
    return (input: unknown) => formatValue(input, format)
  }
  return value
}

function resolveDataProp(type: string, value: unknown, rows: Row[]): unknown {
  if (value === undefined) {
    return rows
  }
  if (Array.isArray(value)) {
    return value
  }
  throw new Error(`Recharts ${type} props.data must be an array; omit props.data to use block rows`)
}

function isGraphSeries(value: GraphSeries | undefined): value is GraphSeries {
  return Boolean(value)
}

export function formatAxisTick(value: unknown): string {
  const text = String(value)
  const isoDate = /^(\d{4})-(\d{2})-(\d{2})(?:T.*)?$/.exec(text)
  if (isoDate) {
    const [, rawYear, rawMonth, rawDay] = isoDate
    const year = Number.parseInt(rawYear, 10)
    const month = Number.parseInt(rawMonth, 10)
    const day = Number.parseInt(rawDay, 10)
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(
      new Date(year, month - 1, day),
    )
  }
  return text.length > 14 ? `${text.slice(0, 13)}...` : text
}

interface TooltipEntry {
  color?: string
  dataKey?: string
  name?: string
  value?: unknown
}

function AccessibleTooltipContent({
  active,
  payload,
  label,
  valueFormatter,
  labelFormatter,
}: {
  active?: boolean
  payload?: TooltipEntry[]
  label?: unknown
  valueFormatter?: unknown
  labelFormatter?: unknown
}) {
  if (!active || !payload?.length) return null

  const renderedLabel = typeof labelFormatter === "function"
    ? labelFormatter(label)
    : formatAxisTick(label)
  return (
    <div className="min-w-32 rounded-lg border border-border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-lg">
      <div className="mb-1.5 font-medium">{String(renderedLabel)}</div>
      <div className="space-y-1">
        {payload.map((entry, index) => {
          const formatted = typeof valueFormatter === "function"
            ? valueFormatter(entry.value, entry.name, entry, index, payload)
            : entry.value
          const displayed = Array.isArray(formatted) ? formatted[0] : formatted
          return (
            <div key={`${entry.dataKey ?? entry.name ?? "series"}-${index}`} className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: entry.color ?? "var(--muted-foreground)" }}
              />
              <span className="min-w-0 flex-1 truncate text-popover-foreground">{entry.name ?? entry.dataKey}</span>
              <span className="font-medium tabular-nums text-popover-foreground">{String(displayed)}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined
}

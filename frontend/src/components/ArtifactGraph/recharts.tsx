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
}

export interface GraphSeries {
  data_key: string
  label?: string
  color?: string
}

export const SERIES_COLORS = [
  "#2563eb",
  "#059669",
  "#d97706",
  "#7c3aed",
  "#dc2626",
  "#0891b2",
  "#4f46e5",
]

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
  return buildNode(tree, { rows, seriesIndex: 0 })
}

export function compileCompactGraphConfig(config: {
  chart_type?: string
  x_key?: string
  y_key?: string
  series?: unknown
  data_label?: string
  y_format?: string
  stacked?: boolean
}): RechartsNode {
  const xKey = config.x_key ?? "date"
  const series = normalizeGraphSeries(config.series, config.y_key, config.data_label)
  const yFormat = (value: unknown) => formatValue(value, config.y_format ?? "compact")
  const colorOf = (index: number) => series[index]?.color ?? SERIES_COLORS[index % SERIES_COLORS.length]

  if (config.chart_type === "pie") {
    return {
      type: "PieChart",
      children: [
        { type: "Tooltip", props: { formatter: yFormat } },
        {
          type: "Pie",
          props: {
            dataKey: series[0]?.data_key ?? "value",
            nameKey: xKey,
            innerRadius: "45%",
            outerRadius: "80%",
            paddingAngle: 2,
          },
        },
        { type: "Legend" },
      ],
    }
  }

  const axes: RechartsNode[] = [
    { type: "CartesianGrid", props: { strokeDasharray: "3 3", stroke: "var(--border)" } },
    { type: "XAxis", props: { dataKey: xKey } },
    { type: "YAxis", props: { tickFormatter: yFormat } },
    { type: "Tooltip", props: { formatter: yFormat } },
    ...(series.length > 1 ? [{ type: "Legend" }] : []),
  ]

  if (config.chart_type === "bar") {
    return {
      type: "BarChart",
      children: [
        ...axes,
        ...series.map((entry, index) => ({
          type: "Bar",
          props: {
            dataKey: entry.data_key,
            name: entry.label ?? entry.data_key,
            fill: colorOf(index),
            stackId: config.stacked ? "stack" : undefined,
          },
        })),
      ],
    }
  }

  if (config.chart_type === "area") {
    return {
      type: "AreaChart",
      children: [
        ...axes,
        ...series.map((entry, index) => ({
          type: "Area",
          props: {
            type: "monotone",
            dataKey: entry.data_key,
            name: entry.label ?? entry.data_key,
            stroke: colorOf(index),
            fill: colorOf(index),
            stackId: config.stacked ? "stack" : undefined,
          },
        })),
      ],
    }
  }

  return {
    type: "LineChart",
    children: [
      ...axes,
      ...series.map((entry, index) => ({
        type: "Line",
        props: {
          type: "monotone",
          dataKey: entry.data_key,
          name: entry.label ?? entry.data_key,
          stroke: colorOf(index),
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
                color: stringValue(item.color),
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

function buildNode(node: RechartsNode, state: { rows: Row[]; seriesIndex: number }, key?: React.Key): React.ReactElement {
  const component = RECHARTS_REGISTRY[node.type]
  if (!component) {
    throw new Error(`Unknown Recharts component "${node.type}"`)
  }

  const seriesIndex = state.seriesIndex
  if (SERIES_TYPES.has(node.type)) {
    state.seriesIndex += 1
  }

  const props: Record<string, unknown> = {}
  for (const [name, value] of Object.entries(node.props ?? {})) {
    props[name] = resolveProp(value)
  }
  const defaulted = applyDefaults(node.type, props, seriesIndex)
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
        <Cell key={`auto-cell-${index}`} fill={SERIES_COLORS[index % SERIES_COLORS.length]} />
      )),
    ]
  }

  return React.createElement(component, defaulted, ...(children ?? []))
}

function applyDefaults(type: string, props: Record<string, unknown>, seriesIndex: number): Record<string, unknown> {
  const out = { ...props }
  const defaultColor = SERIES_COLORS[seriesIndex % SERIES_COLORS.length]
  if (type === "XAxis" || type === "YAxis") {
    out.tick = out.tick ?? { fontSize: 11, fill: "var(--muted-foreground)" }
  }
  if (type === "XAxis") {
    out.tickFormatter = out.tickFormatter ?? defaultTickFormatter
  }
  if (type === "YAxis") {
    out.width = out.width ?? 56
  }
  if (type === "Tooltip") {
    out.labelStyle = out.labelStyle ?? { fontSize: 12 }
    out.contentStyle = out.contentStyle ?? {
      borderRadius: 8,
      borderColor: "var(--border)",
      boxShadow: "0 8px 24px rgb(15 23 42 / 0.12)",
    }
  }
  if (type === "Legend") {
    out.wrapperStyle = out.wrapperStyle ?? { fontSize: 12 }
  }
  if (type === "Line") {
    out.stroke = out.stroke ?? defaultColor
    out.strokeWidth = out.strokeWidth ?? 2
    out.dot = out.dot ?? false
  }
  if (type === "Area") {
    out.stroke = out.stroke ?? defaultColor
    out.fill = out.fill ?? defaultColor
    out.fillOpacity = out.fillOpacity ?? 0.25
  }
  if (type === "Bar") {
    out.fill = out.fill ?? defaultColor
    out.radius = out.radius ?? [4, 4, 0, 0]
  }
  if (type === "Scatter") {
    out.fill = out.fill ?? defaultColor
  }
  return out
}

function resolveProp(value: unknown): unknown {
  if (isRecord(value) && typeof value.$format === "string") {
    const format = value.$format
    return (input: unknown) => formatValue(input, format)
  }
  return value
}

function resolveDataProp(type: string, value: unknown, rows: Row[]): unknown {
  if (value === undefined || isRefValue(value)) {
    return rows
  }
  if (Array.isArray(value)) {
    return value
  }
  throw new Error(`Recharts ${type} props.data must be an array; omit props.data to use block rows`)
}

function isRefValue(value: unknown): boolean {
  return isRecord(value) && typeof value.$ref === "string"
}

function isGraphSeries(value: GraphSeries | undefined): value is GraphSeries {
  return Boolean(value)
}

function defaultTickFormatter(value: unknown): string {
  const text = String(value)
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text.slice(5)
  return text.length > 14 ? `${text.slice(0, 13)}...` : text
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined
}

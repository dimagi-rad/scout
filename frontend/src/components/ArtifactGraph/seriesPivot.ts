import type { GraphSeries } from "./recharts"
import type { Row } from "./types"

export const MAX_DIMENSION_SERIES = 5

interface PivotOptions {
  xKey: string
  yKey: string
  seriesBy: string
  fillMissing: boolean
}

export function pivotSeriesRows(rows: Row[], { xKey, yKey, seriesBy, fillMissing }: PivotOptions): {
  rows: Row[]
  series: GraphSeries[]
} {
  const totals = new Map<unknown, number>()
  const groups = new Map<unknown, Map<unknown, number>>()
  for (const row of rows) {
    for (const key of [xKey, yKey, seriesBy]) {
      if (!Object.hasOwn(row, key)) throw new Error(`Series data is missing field "${key}"`)
    }
    const dimension = row[seriesBy]
    if (dimension !== null && !["string", "number", "boolean"].includes(typeof dimension)) {
      throw new Error(`series_by field "${seriesBy}" must contain scalar values or null`)
    }
    const x = row[xKey]
    const group = groups.get(x) ?? new Map<unknown, number>()
    groups.set(x, group)
    const raw = row[yKey]
    if (raw === null) {
      if (!totals.has(dimension)) totals.set(dimension, 0)
      continue
    }
    let value = NaN
    if (typeof raw === "number") value = raw
    if (typeof raw === "string" && raw.trim() !== "") value = Number(raw)
    if (!Number.isFinite(value)) throw new Error(`Series measure "${yKey}" must contain finite numeric values`)
    totals.set(dimension, (totals.get(dimension) ?? 0) + value)
    group.set(dimension, (group.get(dimension) ?? 0) + value)
  }

  const capped = totals.size > MAX_DIMENSION_SERIES
  const dimensions = capped
    ? [...totals.keys()].sort((a, b) => totals.get(b)! - totals.get(a)!).slice(0, MAX_DIMENSION_SERIES - 1)
    : [...totals.keys()]
  const selected = new Set(dimensions)
  const labels = new Set([...totals.keys()].filter((v) => typeof v === "string") as string[])
  const uniqueLabel = (base: string) => {
    let label = base
    let suffix = 2
    while (labels.has(label)) label = `${base} (${suffix++})`
    labels.add(label)
    return label
  }
  const series: GraphSeries[] = dimensions.map((dimension, index) => {
    let label: string
    if (dimension === null) label = uniqueLabel("(Missing)")
    else if (typeof dimension === "string") label = dimension
    else label = uniqueLabel(String(dimension))
    return { data_key: generatedKey(index, xKey), label }
  })
  if (capped) {
    series.push({
      data_key: generatedKey(series.length, xKey),
      label: uniqueLabel(labels.has("Other") ? "Other (remaining)" : "Other"),
    })
  }

  return {
    series,
    rows: [...groups].map(([x, values]) => {
      const row: Row = { [xKey]: x }
      dimensions.forEach((dimension, index) => {
        row[series[index].data_key] = values.get(dimension) ?? (fillMissing ? 0 : null)
      })
      if (capped) {
        const remaining = [...values].filter(([dimension]) => !selected.has(dimension))
        row[series[series.length - 1].data_key] = remaining.length
          ? remaining.reduce((total, [, value]) => total + value, 0)
          : fillMissing ? 0 : null
      }
      return row
    }),
  }
}

function generatedKey(index: number, xKey: string): string {
  const key = `__series_${index}`
  return key === xKey ? `${key}_value` : key
}

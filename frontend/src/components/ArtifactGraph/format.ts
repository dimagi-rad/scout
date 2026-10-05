import type { FieldMetadata } from "./types"

export function numeric(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

export function formatValue(value: unknown, format?: string, field?: FieldMetadata): string {
  if (value === null || value === undefined) return "-"

  const dataType = field?.data_type?.toLowerCase() ?? ""
  const isTimeDimension = field?.field_type === "time_dimension"
  const hasTimeBucket = isTimeDimension && Boolean(field.granularity)
  const coarseTimeBucket = isTimeDimension
    && ["day", "week", "month", "quarter", "year"].includes(field.granularity ?? "")
  const dateOnly = format === "date" || (!format && (
    coarseTimeBucket || (dataType === "date" && !hasTimeBucket)
  ))
  const dateTime = format === "datetime" || (!format && isTimeDimension && !dateOnly)
  if ((dateOnly || dateTime) && typeof value === "string") {
    const calendarDate = parseIsoDateLocal(value)
    if (!calendarDate) return value
    const timestamp = value.replace(/^(\d{4})-(\d{1,2})-(\d{1,2})[T ]/, (_match, year: string, month: string, day: string) =>
      `${year}-${month.padStart(2, "0")}-${day.padStart(2, "0")}T`)
    const date = dateOnly || /^\d{4}-\d{1,2}-\d{1,2}$/.test(value) ? calendarDate : new Date(timestamp)
    if (Number.isNaN(date.getTime())) return value
    return dateOnly ? date.toLocaleDateString() : date.toLocaleString()
  }

  if (field?.field_type === "dimension" || /^(string|text|varchar|char|character|uuid)/i.test(field?.data_type ?? "")) {
    return String(value)
  }
  const declaredNumeric = field?.field_type === "measure" || /^(number|numeric|decimal|integer|int|bigint|smallint|float|double|real)/i.test(field?.data_type ?? "")
  const numericString = declaredNumeric && typeof value === "string" && safeNumericString(value)
  const numberValue = typeof value === "number" || numericString ? numeric(value) : null
  if (numberValue !== null) {
    const namedFormat = parseNamedFormat(format)
    if (namedFormat.kind === "currency") {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: namedFormat.decimals,
        maximumFractionDigits: namedFormat.decimals ?? (numberValue % 1 === 0 ? 0 : 2),
      }).format(numberValue)
    }
    if (namedFormat.kind === "accounting") {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: "USD",
        currencySign: "accounting",
        minimumFractionDigits: namedFormat.decimals,
        maximumFractionDigits: namedFormat.decimals ?? 2,
      }).format(numberValue)
    }
    if (namedFormat.kind === "percent") {
      return new Intl.NumberFormat(undefined, {
        style: "percent",
        minimumFractionDigits: namedFormat.decimals,
        maximumFractionDigits: namedFormat.decimals ?? 1,
      }).format(numberValue)
    }
    if (namedFormat.kind === "compact" || namedFormat.kind === "abbr") {
      return new Intl.NumberFormat(undefined, {
        notation: "compact",
        minimumFractionDigits: namedFormat.decimals,
        maximumFractionDigits: namedFormat.decimals ?? 1,
      }).format(numberValue)
    }
    if (namedFormat.kind === "number") {
      return new Intl.NumberFormat(undefined, {
        minimumFractionDigits: namedFormat.decimals,
        maximumFractionDigits: namedFormat.decimals,
      }).format(numberValue)
    }
    return new Intl.NumberFormat(undefined).format(numberValue)
  }

  return String(value)
}

export function selectPath(value: unknown, path: string | undefined): unknown {
  if (!path) return undefined
  const parts = path.match(/\[(\d+)\]|[^.[\]]+/g) ?? []
  let current = value
  for (const raw of parts) {
    if (current === null || current === undefined) return undefined
    const indexMatch = raw.match(/^\[(\d+)\]$/)
    const key: string | number = indexMatch ? Number(indexMatch[1]) : raw
    if (Array.isArray(current)) {
      current = typeof key === "number" ? current[key] : undefined
    } else if (isObject(current)) {
      current = current[String(key)]
    } else {
      current = undefined
    }
  }
  return current
}

export function firstNumericKey(row: Record<string, unknown> | undefined): string | undefined {
  return row ? Object.keys(row).find((key) => numeric(row[key]) !== null) : undefined
}

export function pathKey(value: string | undefined): string | undefined {
  return value?.match(/[A-Za-z_][A-Za-z0-9_]*/g)?.at(-1)
}

function safeNumericString(value: string): boolean {
  const match = value.trim().match(/^[+-]?(\d*\.?\d+)(?:[eE][+-]?\d+)?$/)
  if (!match) return false
  const significantDigits = match[1].replace(".", "").replace(/^0+/, "").replace(/0+$/, "")
  if (significantDigits.length > 15) return false
  const number = numeric(value)
  return number !== null && Math.abs(number) <= Number.MAX_SAFE_INTEGER
    && (number !== 0 || significantDigits.length === 0)
}

function parseIsoDateLocal(value: string): Date | null {
  const match = value.match(/^(\d{4})-(\d{1,2})-(\d{1,2})(?:$|[T ])/)
  if (!match) return null
  const [, year, month, day] = match.map(Number)
  const date = new Date(0)
  date.setFullYear(year, month - 1, day)
  date.setHours(0, 0, 0, 0)
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : null
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function parseNamedFormat(format?: string): { kind?: string; decimals?: number } {
  const match = format?.match(/^(number|percent|currency|compact|abbr|accounting)(?:_(\d+))?$/)
  if (!match) return {}
  const decimals = match[2] === undefined ? undefined : Number.parseInt(match[2], 10)
  return { kind: match[1], decimals: Number.isFinite(decimals) ? decimals : undefined }
}

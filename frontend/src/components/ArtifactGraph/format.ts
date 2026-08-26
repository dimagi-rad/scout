export function numeric(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

export function formatValue(value: unknown, format?: string): string {
  if (value === null || value === undefined) return "-"

  if (format === "date" && typeof value === "string") {
    const date = parseIsoDateLocal(value)
    return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString()
  }

  const numberValue = numeric(value)
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

function parseIsoDateLocal(value: string): Date {
  const [year, month, day] = value.split("-").map((part) => Number.parseInt(part, 10))
  return new Date(year, (month || 1) - 1, day || 1)
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

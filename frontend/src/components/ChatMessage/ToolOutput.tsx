import { AlertTriangle, CheckCircle, XCircle, Clock, Database, Hash } from "lucide-react"
import { SqlHighlighter } from "./SqlHighlighter"

// ---- shared helpers ----

function Badge({
  children,
  variant = "default",
}: {
  children: React.ReactNode
  variant?: "default" | "success" | "error" | "warning" | "muted"
}) {
  const cls = {
    default: "bg-muted text-muted-foreground",
    success: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
    error: "bg-red-500/10 text-red-600 dark:text-red-400",
    warning: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
    muted: "bg-muted/50 text-muted-foreground/70",
  }[variant]
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${cls}`}
    >
      {children}
    </span>
  )
}

// ---- query tool ----

interface QueryOutput {
  success: boolean
  data?: {
    columns: string[]
    rows: unknown[][]
    row_count: number
    truncated?: boolean
    sql_executed?: string
    tables_accessed?: string[]
  }
  error?: { code: string; message: string }
  warnings?: string[]
  timing_ms?: number
  schema?: string
}

export function QueryToolOutput({ output }: { output: QueryOutput }) {
  if (!output.success || !output.data) {
    return (
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <XCircle className="w-3.5 h-3.5 text-red-500 shrink-0" />
          <span className="text-red-500 font-medium text-xs">Query failed</span>
          {output.error && <Badge variant="error">{output.error.code}</Badge>}
        </div>
        {output.error && (
          <p className="text-xs text-muted-foreground pl-5">{output.error.message}</p>
        )}
      </div>
    )
  }

  const { columns, rows, row_count, truncated, sql_executed, tables_accessed } = output.data

  return (
    <div className="space-y-3">
      {/* Status row */}
      <div className="flex items-center gap-2 flex-wrap">
        <CheckCircle className="w-3.5 h-3.5 text-emerald-500 shrink-0" />
        <span className="text-xs text-muted-foreground font-medium">Query succeeded</span>
        <Badge variant="success">
          <Hash className="w-2.5 h-2.5 mr-0.5 inline" />
          {row_count} row{row_count !== 1 ? "s" : ""}
        </Badge>
        {output.timing_ms != null && (
          <Badge variant="muted">
            <Clock className="w-2.5 h-2.5 mr-0.5 inline" />
            {output.timing_ms}ms
          </Badge>
        )}
        {output.schema && (
          <Badge variant="muted">
            <Database className="w-2.5 h-2.5 mr-0.5 inline" />
            {output.schema}
          </Badge>
        )}
        {truncated && (
          <Badge variant="warning">
            <AlertTriangle className="w-2.5 h-2.5 mr-0.5 inline" />
            truncated
          </Badge>
        )}
      </div>

      {/* SQL block */}
      {sql_executed && (
        <div className="rounded bg-zinc-950 dark:bg-zinc-900 border border-border/50 px-3 py-2.5 overflow-x-auto">
          <pre className="whitespace-pre-wrap leading-relaxed">
            <SqlHighlighter sql={sql_executed} />
          </pre>
        </div>
      )}

      {/* Warnings */}
      {output.warnings?.map((w, i) => (
        <div key={i} className="flex items-start gap-1.5 text-xs text-amber-600 dark:text-amber-400">
          <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
          {w}
        </div>
      ))}

      {/* Results table */}
      {rows.length > 0 && (
        <div className="overflow-x-auto rounded border border-border/50">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border/50 bg-muted/40">
                {columns.map((col) => (
                  <th
                    key={col}
                    className="px-2.5 py-1.5 text-left font-medium text-muted-foreground whitespace-nowrap"
                  >
                    {col}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr
                  key={ri}
                  className="border-b border-border/30 last:border-0 hover:bg-muted/20 transition-colors"
                >
                  {row.map((cell, ci) => (
                    <td key={ci} className="px-2.5 py-1.5 text-foreground/80 whitespace-nowrap">
                      {cell === null || cell === undefined ? (
                        <span className="text-muted-foreground/50 italic text-[10px]">null</span>
                      ) : (
                        String(cell)
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Tables accessed footer */}
      {tables_accessed && tables_accessed.length > 0 && (
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-[10px] text-muted-foreground/60">Tables:</span>
          {tables_accessed.map((t) => (
            <span
              key={t}
              className="text-[10px] font-mono text-muted-foreground/60 bg-muted/40 rounded px-1"
            >
              {t}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

// ---- describe_table tool ----

interface DescribeTableOutput {
  success: boolean
  data?: {
    name: string
    description?: string
    columns: Array<{
      name: string
      type: string
      nullable?: boolean
      description?: string
    }>
  }
  timing_ms?: number
  schema?: string
}

export function DescribeTableOutput({ output }: { output: DescribeTableOutput }) {
  if (!output.success || !output.data) {
    return <span className="text-xs text-red-500">Failed to describe table</span>
  }
  const { name, description, columns } = output.data
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <Database className="w-3.5 h-3.5 text-blue-400 shrink-0" />
        <span className="text-xs font-mono font-semibold text-foreground/90">{name}</span>
        <Badge variant="muted">{columns.length} columns</Badge>
        {output.timing_ms != null && (
          <Badge variant="muted">
            <Clock className="w-2.5 h-2.5 mr-0.5 inline" />
            {output.timing_ms}ms
          </Badge>
        )}
      </div>
      {description && <p className="text-xs text-muted-foreground pl-5">{description}</p>}
      <div className="overflow-x-auto rounded border border-border/50">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border/50 bg-muted/40">
              <th className="px-2.5 py-1.5 text-left font-medium text-muted-foreground">Column</th>
              <th className="px-2.5 py-1.5 text-left font-medium text-muted-foreground">Type</th>
              <th className="px-2.5 py-1.5 text-left font-medium text-muted-foreground">Nullable</th>
              <th className="px-2.5 py-1.5 text-left font-medium text-muted-foreground">
                Description
              </th>
            </tr>
          </thead>
          <tbody>
            {columns.map((col) => (
              <tr key={col.name} className="border-b border-border/30 last:border-0 hover:bg-muted/20">
                <td className="px-2.5 py-1.5 font-mono text-foreground/80 whitespace-nowrap">
                  {col.name}
                </td>
                <td className="px-2.5 py-1.5 text-blue-400/80 font-mono whitespace-nowrap">
                  {col.type}
                </td>
                <td className="px-2.5 py-1.5">
                  {col.nullable === false ? (
                    <Badge variant="muted">NOT NULL</Badge>
                  ) : (
                    <span className="text-muted-foreground/50 text-[10px]">nullable</span>
                  )}
                </td>
                <td className="px-2.5 py-1.5 text-muted-foreground text-[11px]">
                  {col.description ?? ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---- list_tables tool ----

interface ListTablesOutput {
  success: boolean
  data?: {
    tables: Array<{ name: string; type?: string; description?: string; row_count?: number }>
    note?: string
  }
  timing_ms?: number
}

export function ListTablesOutput({ output }: { output: ListTablesOutput }) {
  if (!output.success || !output.data)
    return <span className="text-xs text-red-500">Failed to list tables</span>
  const { tables, note } = output.data
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Database className="w-3.5 h-3.5 text-blue-400 shrink-0" />
        <span className="text-xs text-muted-foreground font-medium">{tables.length} tables</span>
        {output.timing_ms != null && (
          <Badge variant="muted">
            <Clock className="w-2.5 h-2.5 mr-0.5 inline" />
            {output.timing_ms}ms
          </Badge>
        )}
      </div>
      {note && <p className="text-xs text-amber-500 pl-5">{note}</p>}
      <div className="flex flex-wrap gap-1">
        {tables.map((t) => (
          <span
            key={t.name}
            className="text-[11px] font-mono bg-muted/50 border border-border/40 rounded px-1.5 py-0.5 text-foreground/70"
          >
            {t.name}
            {t.row_count != null && (
              <span className="text-muted-foreground/50 ml-1">({t.row_count.toLocaleString()})</span>
            )}
          </span>
        ))}
      </div>
    </div>
  )
}

// ---- get_metadata tool ----

interface GetMetadataOutput {
  success: boolean
  data?: { tables: unknown[] }
  timing_ms?: number
  schema?: string
}

export function GetMetadataOutput({ output }: { output: GetMetadataOutput }) {
  if (!output.success || !output.data)
    return <span className="text-xs text-red-500">Failed to get metadata</span>
  const tableCount = Array.isArray(output.data.tables) ? output.data.tables.length : 0
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <CheckCircle className="w-3.5 h-3.5 text-emerald-500 shrink-0" />
      <span className="text-xs text-muted-foreground">Metadata loaded</span>
      <Badge variant="muted">{tableCount} tables</Badge>
      {output.schema && (
        <Badge variant="muted">
          <Database className="w-2.5 h-2.5 mr-0.5 inline" />
          {output.schema}
        </Badge>
      )}
      {output.timing_ms != null && (
        <Badge variant="muted">
          <Clock className="w-2.5 h-2.5 mr-0.5 inline" />
          {output.timing_ms}ms
        </Badge>
      )}
    </div>
  )
}

// ---- output parser ----

function parseOutput(output: unknown): unknown {
  if (typeof output === "string") {
    // MCP wraps results as [{'type':'text','text':'...json...'}] with single quotes
    try {
      // Replace Python-style single-quote dict with JSON
      const jsonLike = output.replace(/'/g, '"')
      const arr = JSON.parse(jsonLike)
      if (Array.isArray(arr) && arr[0]?.text) return JSON.parse(arr[0].text)
    } catch {
      /* ignore */
    }
    try {
      return JSON.parse(output)
    } catch {
      return output
    }
  }
  // Handle the MCP envelope array directly (already parsed objects)
  if (
    Array.isArray(output) &&
    output[0]?.type === "text" &&
    typeof output[0]?.text === "string"
  ) {
    try {
      return JSON.parse(output[0].text)
    } catch {
      return output
    }
  }
  return output
}

// ---- dispatch ----

export function renderToolOutput(toolName: string, rawOutput: unknown): React.ReactNode | null {
  const output = parseOutput(rawOutput)
  if (output == null) return null

  switch (toolName) {
    case "query":
      return <QueryToolOutput output={output as QueryOutput} />
    case "describe_table":
      return <DescribeTableOutput output={output as DescribeTableOutput} />
    case "list_tables":
      return <ListTablesOutput output={output as ListTablesOutput} />
    case "get_metadata":
      return <GetMetadataOutput output={output as GetMetadataOutput} />
    default:
      return null
  }
}

import { useMemo } from "react"
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter"
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism"
import { format as formatSql } from "sql-formatter"
import { cn } from "@/lib/utils"

export interface SqlBlockProps {
  sql: string
  showLineNumbers?: boolean
  maxHeight?: string
  className?: string
  "data-testid"?: string
}

const CODE_FONT =
  'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace'

function formatSqlForDisplay(sql: string): string {
  try {
    return formatSql(sql, { language: "postgresql", keywordCase: "upper" })
  } catch {
    return sql
  }
}

export function SqlBlock({
  sql,
  showLineNumbers = false,
  maxHeight = "26rem",
  className,
  "data-testid": testId,
}: SqlBlockProps) {
  const formattedSql = useMemo(() => formatSqlForDisplay(sql), [sql])
  return (
    <div
      className={cn("overflow-hidden rounded-md border bg-background", className)}
      data-testid={testId}
    >
      <SyntaxHighlighter
        language="sql"
        style={oneLight}
        showLineNumbers={showLineNumbers}
        wrapLongLines
        customStyle={{
          margin: 0,
          maxHeight,
          overflow: "auto",
          background: "transparent",
          fontSize: "0.75rem",
          lineHeight: "1.25rem",
        }}
        codeTagProps={{ style: { fontFamily: CODE_FONT } }}
        lineNumberStyle={{
          minWidth: "2.75em",
          paddingRight: "1em",
          color: "var(--muted-foreground)",
          textAlign: "right",
          userSelect: "none",
        }}
      >
        {formattedSql}
      </SyntaxHighlighter>
    </div>
  )
}

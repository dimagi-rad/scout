import { Fragment } from "react"

const SQL_KEYWORDS =
  /\b(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|ON|AS|AND|OR|NOT|NULL|IS|IN|LIKE|BETWEEN|DISTINCT|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|OFFSET|UNION|ALL|WITH|CASE|WHEN|THEN|ELSE|END|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|ALTER|DROP|TABLE|INDEX|VIEW|EXISTS|COUNT|SUM|AVG|MIN|MAX|ROUND|CAST|COALESCE|NULLIF|OVER|PARTITION\s+BY|ROW_NUMBER|RANK|DENSE_RANK|LAG|LEAD|FIRST_VALUE|LAST_VALUE|EXTRACT|DATE_TRUNC|NOW|TRUE|FALSE|ASC|DESC|NULLS|FIRST|LAST|BY|RETURNS|LANGUAGE|DECLARE|BEGIN|COMMIT|ROLLBACK)\b/gi

const SQL_STRINGS = /'[^']*'/g
const SQL_NUMBERS = /\b\d+(\.\d+)?\b/g
const SQL_COMMENTS = /--.*/g

interface Token {
  text: string
  type: "keyword" | "string" | "number" | "comment" | "plain"
}

function tokenize(sql: string): Token[] {
  const matches: { start: number; end: number; type: Token["type"] }[] = []

  for (const [regex, type] of [
    [SQL_COMMENTS, "comment"],
    [SQL_STRINGS, "string"],
    [SQL_KEYWORDS, "keyword"],
    [SQL_NUMBERS, "number"],
  ] as const) {
    const re = new RegExp(regex.source, regex.flags)
    let m: RegExpExecArray | null
    while ((m = re.exec(sql)) !== null) {
      matches.push({ start: m.index, end: m.index + m[0].length, type })
    }
  }

  // Sort by start, prefer longer matches, remove overlaps
  matches.sort((a, b) => a.start - b.start || b.end - a.end)
  const filtered: typeof matches = []
  let cursor = 0
  for (const m of matches) {
    if (m.start >= cursor) {
      filtered.push(m)
      cursor = m.end
    }
  }

  // Build tokens including plain text gaps
  const tokens: Token[] = []
  let pos = 0
  for (const m of filtered) {
    if (m.start > pos) tokens.push({ text: sql.slice(pos, m.start), type: "plain" })
    tokens.push({ text: sql.slice(m.start, m.end), type: m.type })
    pos = m.end
  }
  if (pos < sql.length) tokens.push({ text: sql.slice(pos), type: "plain" })
  return tokens
}

const TYPE_CLASSES: Record<Token["type"], string> = {
  keyword: "text-blue-400 font-semibold",
  string: "text-amber-400",
  number: "text-emerald-300",
  comment: "text-muted-foreground/60 italic",
  plain: "",
}

export function SqlHighlighter({ sql }: { sql: string }) {
  const tokens = tokenize(sql)
  return (
    <code className="font-mono text-xs leading-relaxed text-zinc-200">
      {tokens.map((t, i) => (
        <Fragment key={i}>
          {t.type === "plain" ? (
            t.text
          ) : (
            <span className={TYPE_CLASSES[t.type]}>{t.text}</span>
          )}
        </Fragment>
      ))}
    </code>
  )
}

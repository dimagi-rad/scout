// Leading characters that make Excel/Sheets evaluate a cell as a formula (OWASP CSV injection).
const FORMULA_PREFIX = /^[=+\-@\t\r]/

// Numeric text (Postgres numeric arrives as a string) cannot be a formula; prefixing would corrupt it.
const PLAIN_NUMBER = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/

function csvCell(value: unknown): string {
  if (value === null || value === undefined) return ""
  let text: string
  if (typeof value === "object") {
    try {
      text = JSON.stringify(value)
    } catch {
      text = String(value)
    }
  } else {
    text = String(value)
  }
  const isNumeric = typeof value === "number" || typeof value === "bigint" || PLAIN_NUMBER.test(text)
  // Fullwidth forms (＝ ＋ － ＠) are formula starters in some CJK-locale spreadsheets.
  if (!isNumeric && FORMULA_PREFIX.test(text.normalize("NFKC"))) {
    text = `'${text}`
  }
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

export function toCsv(columns: readonly string[], rows: readonly (readonly unknown[])[]): string {
  const lines = [columns, ...rows].map((row) => row.map(csvCell).join(","))
  return lines.join("\r\n") + "\r\n"
}

export function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60)
    .replace(/-+$/, "")
}

export function csvFilename(title: string | undefined, now: Date = new Date()): string {
  const stamp = now.toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-")
  return `${slugify(title ?? "") || "scout-query"}-${stamp}.csv`
}

export function downloadCsv(filename: string, csv: string): void {
  // BOM so Excel detects UTF-8.
  const blob = new Blob(["﻿", csv], { type: "text/csv;charset=utf-8" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  try {
    a.click()
  } finally {
    document.body.removeChild(a)
    // Revoking synchronously can cancel the download in older Safari/Firefox.
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}

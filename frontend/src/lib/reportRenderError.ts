import type { Breadcrumb } from "@sentry/react"
import * as Sentry from "@sentry/react"

export type RenderErrorSource = "sandbox" | "boundary"

export interface RenderErrorReport {
  source: RenderErrorSource
  name: string
  message: string
  stack?: string
  artifactId?: string
  artifactVersion?: number
  /** Which sandbox renderer step failed, e.g. "React Render Error". */
  stage?: string
}

const MAX_MESSAGE_LENGTH = 300
const MAX_STACK_FRAMES = 10
// A sandboxed artifact runs generated code and can post any number of distinct
// errors; the per-key dedupe alone would not stop it from flooding Sentry. The
// budget is per source so sandbox noise cannot starve app boundary reports.
const MAX_REPORTS_PER_SOURCE = 20

// V8 frames ("    at fn (url:1:2)") and SpiderMonkey/JSC frames ("fn@url:1:2").
// The header line of a V8 stack repeats the message, so it is not kept.
const STACK_FRAME = /^\s+at\s|^[^\s]*@\S+:\d+:\d+$/

const reported = new Set<string>()
const reportCounts = new Map<RenderErrorSource, number>()

// Error text can echo customer values (JSON.parse snippets, SQL literals such as
// `invalid input syntax for type integer: "..."`), so quoted spans are dropped.
// It spans first quote to last on the line: pairing quotes would leave the values
// between a JSON snippet's own quotes (`"{"name":"Alice"}"`) exposed.
function redactQuoted(text: string): string {
  return text.replace(/["'`][^\n]*["'`]/g, "\"…\"")
}

function safeText(text: string, maxLength = MAX_MESSAGE_LENGTH): string {
  const redacted = redactQuoted(text)
  return redacted.length > maxLength ? `${redacted.slice(0, maxLength)}…` : redacted
}

function safeStack(stack: string | undefined): string | undefined {
  if (!stack) return undefined
  const frames = stack.split("\n").filter((line) => STACK_FRAME.test(line))
  return frames.slice(0, MAX_STACK_FRAMES).map(redactQuoted).join("\n") || undefined
}

/**
 * Sentry `beforeBreadcrumb` hook. Console breadcrumbs hold raw error text, such
 * as the message an ErrorBoundary logs before it is redacted here, and every
 * later event in the session would carry them.
 */
export function dropConsoleBreadcrumb(breadcrumb: Breadcrumb): Breadcrumb | null {
  return breadcrumb.category === "console" ? null : breadcrumb
}

/**
 * Report an artifact or app render failure with only the error class, a redacted
 * message and a truncated stack. Artifact data, query results and component state
 * are never attached. Each distinct failure is sent once per session.
 */
export function reportRenderError(report: RenderErrorReport): void {
  // Keyed on the raw text so crashes that redact to the same message stay distinct.
  const key = [report.source, report.artifactId, report.artifactVersion, report.name, report.message]
    .join("|")
  const count = reportCounts.get(report.source) ?? 0
  if (reported.has(key) || count >= MAX_REPORTS_PER_SOURCE) return
  reported.add(key)
  reportCounts.set(report.source, count + 1)

  const name = safeText(report.name || "Error", 80)
  const message = safeText(report.message || "")
  const error = new Error(message)
  error.name = name
  const stack = safeStack(report.stack)
  error.stack = stack ? `${name}: ${message}\n${stack}` : `${name}: ${message}`

  Sentry.withScope((scope) => {
    scope.setTag("render_error_source", report.source)
    if (report.stage) scope.setTag("render_error_stage", safeText(report.stage, 80))
    if (report.artifactId) scope.setTag("artifact_id", report.artifactId)
    if (report.artifactVersion !== undefined) {
      scope.setTag("artifact_version", String(report.artifactVersion))
    }
    Sentry.captureException(error)
  })
}

export function resetReportedRenderErrorsForTests(): void {
  reported.clear()
  reportCounts.clear()
}

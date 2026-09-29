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

// The name comes from the sandboxed artifact and becomes the Sentry error class,
// so anything but a plain identifier could carry text or split Sentry grouping.
export const SAFE_ERROR_NAME = /^[\w$.]{1,80}$/

// Sentry's parser skips any line matching /\S*Error: /.
const STACK_HEADER = "Error: render error"

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
  // Collapsed before redaction so a quoted value broken across lines is still
  // dropped, and so the text cannot pose as a stack frame of its own.
  const redacted = redactQuoted(text.replace(/\s*[\r\n\v\f\u2028\u2029]+\s*/g, " "))
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
  const key = [
    report.source,
    report.artifactId,
    report.artifactVersion,
    report.stage,
    report.name,
    report.message,
  ].join("|")
  const count = reportCounts.get(report.source) ?? 0
  if (reported.has(key) || count >= MAX_REPORTS_PER_SOURCE) return
  reported.add(key)
  reportCounts.set(report.source, count + 1)

  // A boundary gets the raw thrown value, so `name` can be undefined, which
  // RegExp.test would read as the valid identifier "undefined".
  const name =
    typeof report.name === "string" && SAFE_ERROR_NAME.test(report.name) ? report.name : "Error"
  const message = safeText(report.message || "")
  const error = new Error(message)
  error.name = name
  const stack = safeStack(report.stack)
  // The header is fixed text, not "name: message": Sentry takes both from the error
  // itself, and parses a header whose name doesn't end in "Error" as a frame, so
  // artifact text there like "x@https://evil.example/a.js:2:2" would become a fake
  // frame. A header is still needed because Sentry drops the first stack line of a
  // "Minified React error", which would otherwise be a real frame.
  error.stack = stack ? `${STACK_HEADER}\n${stack}` : ""

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

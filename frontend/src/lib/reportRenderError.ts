import * as Sentry from "@sentry/react"

export type RenderErrorSource = "sandbox" | "boundary"

export interface RenderErrorReport {
  source: RenderErrorSource
  name: string
  message: string
  stack?: string
  artifactId?: string
  artifactVersion?: number
  /** Which sandbox renderer step failed, e.g. "Data Fetch Error". */
  stage?: string
}

const MAX_MESSAGE_LENGTH = 300
const MAX_STACK_FRAMES = 10
// A sandboxed artifact runs generated code and can post any number of distinct
// errors; the per-key dedupe alone would not stop it from flooding Sentry.
const MAX_REPORTS_PER_PAGE = 20

// V8 frames ("    at fn (url:1:2)") and SpiderMonkey/JSC frames ("fn@url:1:2").
// The header line of a V8 stack repeats the message, so it is not kept.
const STACK_FRAME = /^\s+at\s|^[^\s]*@\S+:\d+:\d+$/

const reported = new Set<string>()

// Error text can echo customer values (JSON.parse snippets, SQL literals such as
// `invalid input syntax for type integer: "..."`), so quoted spans are dropped.
// It spans first quote to last on the line: pairing quotes would leave the values
// between a JSON snippet's own quotes (`"{"name":"Alice"}"`) exposed.
function redactQuoted(text: string): string {
  return text.replace(/["'`][^\n]*["'`]/g, "\"…\"")
}

function safeMessage(message: string): string {
  const redacted = redactQuoted(message)
  return redacted.length > MAX_MESSAGE_LENGTH
    ? `${redacted.slice(0, MAX_MESSAGE_LENGTH)}…`
    : redacted
}

function safeStack(stack: string | undefined): string | undefined {
  if (!stack) return undefined
  const frames = stack.split("\n").filter((line) => STACK_FRAME.test(line))
  return frames.slice(0, MAX_STACK_FRAMES).map(redactQuoted).join("\n") || undefined
}

/**
 * Report an artifact or app render failure with only the error class, a redacted
 * message and a truncated stack. Artifact data, query results and component state
 * are never attached. Each distinct failure is sent once per page load.
 */
export function reportRenderError(report: RenderErrorReport): void {
  const name = safeMessage(report.name || "Error").slice(0, 80)
  const message = safeMessage(report.message || "")
  const stage = report.stage ? safeMessage(report.stage).slice(0, 80) : undefined
  const key = [report.source, report.artifactId, report.artifactVersion, stage, name, message]
    .join("|")
  if (reported.has(key) || reported.size >= MAX_REPORTS_PER_PAGE) return
  reported.add(key)

  const error = new Error(message)
  error.name = name
  const stack = safeStack(report.stack)
  error.stack = stack ? `${name}: ${message}\n${stack}` : `${name}: ${message}`

  Sentry.withScope((scope) => {
    // The console breadcrumb for this same failure carries the unredacted message.
    scope.addEventProcessor((event) => {
      event.breadcrumbs = event.breadcrumbs?.filter((crumb) => crumb.category !== "console")
      return event
    })
    scope.setTag("render_error_source", report.source)
    if (stage) scope.setTag("render_error_stage", stage)
    if (report.artifactId) scope.setTag("artifact_id", report.artifactId)
    if (report.artifactVersion !== undefined) {
      scope.setTag("artifact_version", String(report.artifactVersion))
    }
    Sentry.captureException(error)
  })
}

export function resetReportedRenderErrorsForTests(): void {
  reported.clear()
}

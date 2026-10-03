import * as Sentry from "@sentry/react"
import type { Breadcrumb, ErrorEvent, EventHint, StackFrame } from "@sentry/react"
import { safeText } from "./reportRenderError"

// Expected responses the UI already handles: signed out, no access, and the
// busy notice. Reporting them would only bury real failures.
const EXPECTED_API_STATUSES = new Set([401, 403, 503])

const EXTENSION_URL = /^(?:chrome|moz|safari|safari-web|ms-browser)-extension:\/\//

const NOISE_MESSAGES = [
  /ResizeObserver loop/,
  // Aborted and dropped requests, as each browser words them.
  /^Failed to fetch$/,
  /^NetworkError when attempting to fetch resource\.?$/,
  /^Load failed$/,
  /^The (?:user aborted a request|operation was aborted)\.?$/,
  /^signal is aborted without reason$/,
  /^Fetch is aborted$/,
]

// Everything else Sentry or a caller might attach (extra, unknown contexts) can
// hold raw values such as ApiError.body or a rejected object's fields.
const KEPT_CONTEXTS = new Set(["app", "browser", "os", "device", "culture", "runtime", "react"])

/** Drops the query string and fragment, where tokens and OAuth codes travel. */
export function stripUrl<T>(url: T): T {
  if (typeof url !== "string") return url
  const cut = url.search(/[?#]/)
  return (cut === -1 ? url : url.slice(0, cut)) as T
}

function errorName(error: unknown): string | undefined {
  return error !== null && typeof error === "object" && "name" in error
    ? String((error as { name: unknown }).name)
    : undefined
}

function errorStatus(error: unknown): number | undefined {
  if (error === null || typeof error !== "object" || !("status" in error)) return undefined
  const { status } = error as { status: unknown }
  return typeof status === "number" ? status : undefined
}

function eventMessages(event: ErrorEvent, hint: EventHint): string[] {
  const messages = (event.exception?.values ?? []).map((value) => value.value ?? "")
  if (event.message) messages.push(event.message)
  const original = hint.originalException
  if (original instanceof Error) messages.push(original.message)
  else if (typeof original === "string") messages.push(original)
  return messages
}

function eventFrames(event: ErrorEvent): StackFrame[] {
  return (event.exception?.values ?? []).flatMap((value) => value.stacktrace?.frames ?? [])
}

/** True for errors that are not Scout bugs: browser quirks, extensions, aborts, expected API statuses. */
export function isNoiseEvent(event: ErrorEvent, hint: EventHint = {}): boolean {
  const original = hint.originalException
  if (errorName(original) === "AbortError") return true
  if (event.exception?.values?.some((value) => value.type === "AbortError")) return true

  const status = errorStatus(original)
  if (errorName(original) === "ApiError" && status !== undefined && EXPECTED_API_STATUSES.has(status)) {
    return true
  }

  if (eventMessages(event, hint).some((message) => NOISE_MESSAGES.some((re) => re.test(message)))) {
    return true
  }

  const frames = eventFrames(event)
  return frames.some((frame) => EXTENSION_URL.test(frame.filename ?? frame.abs_path ?? ""))
}

/**
 * Sentry `beforeBreadcrumb` hook. Console breadcrumbs hold raw error text and
 * logged values, and request breadcrumbs hold full URLs.
 */
export function scrubBreadcrumb(breadcrumb: Breadcrumb): Breadcrumb | null {
  if (breadcrumb.category === "console") return null
  if (!breadcrumb.data) return breadcrumb
  const data = { ...breadcrumb.data }
  for (const key of ["url", "from", "to"]) {
    if (key in data) data[key] = stripUrl(data[key])
  }
  return { ...breadcrumb, data }
}

/** Removes request bodies, cookies, headers, query strings, email and raw values from an event. */
export function scrubEvent(event: ErrorEvent): ErrorEvent {
  if (event.request) {
    const userAgent = event.request.headers?.["User-Agent"]
    event.request = {
      url: stripUrl(event.request.url),
      ...(userAgent ? { headers: { "User-Agent": userAgent } } : {}),
    }
  }

  event.user = event.user?.id !== undefined ? { id: event.user.id } : undefined
  delete event.extra

  if (event.contexts) {
    event.contexts = Object.fromEntries(
      Object.entries(event.contexts).filter(([key]) => KEPT_CONTEXTS.has(key)),
    )
  }

  // Error text can echo customer values, such as SQL literals or chat text an API quotes back.
  if (event.message) event.message = safeText(event.message)
  for (const value of event.exception?.values ?? []) {
    if (value.value) value.value = safeText(value.value)
    for (const frame of value.stacktrace?.frames ?? []) {
      delete frame.vars
    }
  }

  if (event.breadcrumbs) {
    event.breadcrumbs = event.breadcrumbs
      .map(scrubBreadcrumb)
      .filter((breadcrumb): breadcrumb is Breadcrumb => breadcrumb !== null)
  }
  return event
}

export function beforeSend(event: ErrorEvent, hint: EventHint): ErrorEvent | null {
  return isNoiseEvent(event, hint) ? null : scrubEvent(event)
}

/** Errors only: no tracing, no replay, no default PII. A no-op without a DSN. */
export function initSentry(): void {
  const dsn = import.meta.env.VITE_SENTRY_DSN as string | undefined
  if (!dsn) return
  Sentry.init({
    dsn,
    environment:
      (import.meta.env.VITE_SENTRY_ENVIRONMENT as string | undefined) ?? import.meta.env.MODE,
    release: import.meta.env.VITE_SENTRY_RELEASE as string | undefined,
    sendDefaultPii: false,
    denyUrls: [EXTENSION_URL],
    beforeBreadcrumb: scrubBreadcrumb,
    beforeSend,
  })
}

export function setSentryUser(userId: string | undefined): void {
  Sentry.setUser(userId ? { id: userId } : null)
}

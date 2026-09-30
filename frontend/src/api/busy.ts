/**
 * The one client-side handler for "Scout is busy" (a database, pool or Cube
 * connection limit was reached; see apps/common/capacity.py).
 *
 * The backend answers with HTTP 503, a `Retry-After` header and
 * `{"error": "busy"}`, or mid-chat with a `retryable-error` part whose reason is
 * "busy". Either way the user sees one calm notice (BusyNotice) while a few
 * retries back off, and then a manual Retry instead of an error.
 */

export const BUSY_MAX_AUTO_RETRIES = 3
const BASE_DELAY_MS = 1000
const MAX_DELAY_MS = 30_000

export type ManualDecision = "retry" | "dismiss"

export interface BusySnapshot {
  /** Requests waiting out a backoff before their next automatic try. */
  retrying: number
  /** Requests that gave up retrying and wait for the user's Retry or Dismiss. */
  waiting: number
}

export function isBusyBody(body: unknown): boolean {
  return (
    typeof body === "object" &&
    body !== null &&
    (body as { error?: unknown }).error === "busy"
  )
}

/** Honour the server's `Retry-After` (seconds); otherwise back off exponentially. */
export function busyRetryDelayMs(retryAfter: string | number | null | undefined, attempt: number): number {
  const seconds = typeof retryAfter === "number" ? retryAfter : Number.parseFloat(retryAfter ?? "")
  const delay = Number.isFinite(seconds) && seconds >= 0
    ? seconds * 1000
    : BASE_DELAY_MS * 2 ** Math.max(0, attempt - 1)
  return Math.min(delay, MAX_DELAY_MS)
}

type Listener = () => void

export function createBusyTracker() {
  const retrying = new Set<symbol>()
  const waiters = new Map<symbol, (decision: ManualDecision) => void>()
  const listeners = new Set<Listener>()
  let snapshot: BusySnapshot = { retrying: 0, waiting: 0 }

  function publish() {
    snapshot = { retrying: retrying.size, waiting: waiters.size }
    listeners.forEach((listener) => listener())
  }

  function resolveAll(decision: ManualDecision) {
    const pending = [...waiters.values()]
    waiters.clear()
    publish()
    pending.forEach((resolve) => resolve(decision))
  }

  return {
    subscribe(listener: Listener) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    getSnapshot: () => snapshot,
    startRetry(token: symbol) {
      retrying.add(token)
      publish()
    },
    settle(token: symbol) {
      if (retrying.delete(token)) publish()
    },
    /** Park `token` until the user chooses; its backoff, if any, is over. */
    awaitManualRetry(token: symbol): Promise<ManualDecision> {
      retrying.delete(token)
      return new Promise((resolve) => {
        waiters.set(token, resolve)
        publish()
      })
    },
    /** Drop `token` without asking the user, e.g. when its chat is closed. */
    release(token: symbol) {
      const resolve = waiters.get(token)
      waiters.delete(token)
      retrying.delete(token)
      publish()
      resolve?.("dismiss")
    },
    retryAll: () => resolveAll("retry"),
    dismissAll: () => resolveAll("dismiss"),
  }
}

export type BusyTracker = ReturnType<typeof createBusyTracker>

export const busyTracker = createBusyTracker()

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

async function busyBody(res: Response): Promise<boolean> {
  if (res.status !== 503) return false
  const body: unknown = await res.clone().json().catch(() => undefined)
  return isBusyBody(body)
}

/**
 * Run `send` and ride out "busy" answers: back off and retry automatically when
 * `autoRetry` is set, then park the request until the user picks Retry or
 * Dismiss. A dismissed request resolves with the busy response for the caller to
 * handle as an ordinary failure.
 */
export async function fetchWithBusyRetry(
  send: () => Promise<Response>,
  { autoRetry, tracker = busyTracker }: { autoRetry: boolean; tracker?: BusyTracker },
): Promise<Response> {
  const token = Symbol("busy-request")
  let autoRetries = autoRetry ? 0 : BUSY_MAX_AUTO_RETRIES
  try {
    for (;;) {
      const res = await send()
      if (!(await busyBody(res))) return res
      if (autoRetries < BUSY_MAX_AUTO_RETRIES) {
        autoRetries += 1
        tracker.startRetry(token)
        await sleep(busyRetryDelayMs(res.headers.get("Retry-After"), autoRetries))
        continue
      }
      if ((await tracker.awaitManualRetry(token)) === "dismiss") return res
    }
  } finally {
    tracker.settle(token)
  }
}

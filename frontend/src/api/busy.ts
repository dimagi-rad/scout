/**
 * The one client-side handler for "Scout is busy" (a database, pool or Cube
 * connection limit was reached; see apps/common/capacity.py).
 *
 * The backend answers with HTTP 503, a `Retry-After` header and
 * `{"error": "busy", "message": ...}`, or mid-chat with a `retryable-error` part
 * whose reason is "busy". Reads back off and retry a few times while BusyNotice
 * says "retrying"; if Scout is still busy after that, the request fails with the
 * friendly busy message and BusyNotice offers a manual Retry.
 */

export const BUSY_MAX_AUTO_RETRIES = 3
export const BUSY_MESSAGE = "Scout is busy right now. Please try again in a few seconds."
const BASE_DELAY_MS = 1000
const MAX_DELAY_MS = 30_000

export interface BusySnapshot {
  /** Requests waiting out a backoff before their next automatic try. */
  retrying: number
  /** A request gave up while Scout was busy, and nothing has succeeded since. */
  stillBusy: boolean
}

export function isBusyBody(body: unknown): boolean {
  return (
    typeof body === "object" &&
    body !== null &&
    (body as { error?: unknown }).error === "busy"
  )
}

/**
 * Honour the server's `Retry-After` (seconds); otherwise back off exponentially.
 * Jitter spreads out clients that were all turned away in the same instant.
 */
export function busyRetryDelayMs(
  retryAfter: string | number | null | undefined,
  attempt: number,
  random: () => number = Math.random,
): number {
  const seconds = typeof retryAfter === "number" ? retryAfter : Number.parseFloat(retryAfter ?? "")
  const base = Number.isFinite(seconds) && seconds >= 0
    ? seconds * 1000
    : BASE_DELAY_MS * 2 ** Math.max(0, attempt - 1)
  return Math.min(base * (0.8 + random() * 0.4), MAX_DELAY_MS)
}

type Listener = () => void

export function createBusyTracker() {
  const retrying = new Set<symbol>()
  const listeners = new Set<Listener>()
  let stillBusy = false
  let snapshot: BusySnapshot = { retrying: 0, stillBusy: false }

  function publish() {
    if (snapshot.retrying === retrying.size && snapshot.stillBusy === stillBusy) return
    snapshot = { retrying: retrying.size, stillBusy }
    listeners.forEach((listener) => listener())
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
      retrying.delete(token)
      publish()
    },
    gaveUp() {
      stillBusy = true
      publish()
    },
    recovered() {
      stillBusy = false
      publish()
    },
  }
}

export type BusyTracker = ReturnType<typeof createBusyTracker>

export const busyTracker = createBusyTracker()

function sleep(ms: number, signal?: AbortSignal | null): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason)
      return
    }
    const onAbort = () => {
      clearTimeout(timer)
      reject(signal?.reason)
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort)
      resolve()
    }, ms)
    signal?.addEventListener("abort", onAbort, { once: true })
  })
}

export async function isBusyResponse(res: Response): Promise<boolean> {
  if (res.status !== 503) return false
  const body: unknown = await res.clone().json().catch(() => undefined)
  return isBusyBody(body)
}

/**
 * Run `send`, backing off and retrying up to BUSY_MAX_AUTO_RETRIES times while
 * the answer is "busy" (only when `autoRetry` is set). Returns the last response
 * either way, so the caller's normal error handling sees a busy failure.
 */
export async function fetchWithBusyRetry(
  send: () => Promise<Response>,
  {
    autoRetry,
    signal,
    tracker = busyTracker,
  }: { autoRetry: boolean; signal?: AbortSignal | null; tracker?: BusyTracker },
): Promise<Response> {
  const token = Symbol("busy-request")
  const maxRetries = autoRetry ? BUSY_MAX_AUTO_RETRIES : 0
  try {
    for (let retries = 0; ; retries += 1) {
      const res = await send()
      if (!(await isBusyResponse(res))) {
        if (res.ok) tracker.recovered()
        return res
      }
      if (retries >= maxRetries) {
        tracker.gaveUp()
        return res
      }
      tracker.startRetry(token)
      await sleep(busyRetryDelayMs(res.headers.get("Retry-After"), retries + 1), signal)
    }
  } finally {
    tracker.settle(token)
  }
}

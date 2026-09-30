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
const MIN_DELAY_MS = 500
const MAX_DELAY_MS = 30_000
// Honour the server's Retry-After, but never let a proxy's hour-long value hang a request.
const MAX_RETRY_AFTER_MS = 60_000
/** apps/common/capacity.py's Retry-After, for busy answers whose header is unreachable. */
export const BUSY_RETRY_AFTER_SECONDS = 5

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

/** `/health/` has its own `{status, checks}` shape; "busy" means reachable but full. */
export function isBusyHealthBody(body: unknown): boolean {
  return (
    typeof body === "object" &&
    body !== null &&
    (body as { status?: unknown }).status === "busy"
  )
}

/**
 * Honour the server's `Retry-After` (seconds); otherwise back off exponentially.
 * Jitter only ever adds delay, so no retry lands before the server asked (up to a
 * 60s ceiling), and spreads out clients that were all turned away together.
 */
export function busyRetryDelayMs(
  retryAfter: string | number | null | undefined,
  attempt: number,
  random: () => number = Math.random,
): number {
  const seconds = typeof retryAfter === "number" ? retryAfter : Number.parseFloat(retryAfter ?? "")
  if (Number.isFinite(seconds) && seconds >= 0) {
    const asked = Math.max(seconds * 1000, MIN_DELAY_MS)
    return Math.min(asked * (1 + random() * 0.4), MAX_RETRY_AFTER_MS)
  }
  const fallback = BASE_DELAY_MS * 2 ** Math.max(0, attempt - 1)
  return Math.min(fallback * (1 + random() * 0.4), MAX_DELAY_MS)
}

type Listener = () => void

export function createBusyTracker() {
  const retrying = new Set<symbol>()
  const listeners = new Set<Listener>()
  // The retry gate has two sources: reads mid-retry after seeing busy (released
  // however their chain ends, abort or error included), and a read that gave up
  // (latched with the notice until a read succeeds). Dismissing the notice hides
  // it without re-arming retries against a full server.
  const holders = new Set<symbol>()
  let gaveUpLatched = false
  let noticeShown = false
  let dismissed = false
  let snapshot: BusySnapshot = { retrying: 0, stillBusy: false }

  function publish() {
    if (snapshot.retrying === retrying.size && snapshot.stillBusy === noticeShown) return
    snapshot = { retrying: retrying.size, stillBusy: noticeShown }
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
    isHoldingOff: () => gaveUpLatched || holders.size > 0,
    startRetry(token: symbol) {
      retrying.add(token)
      publish()
    },
    settle(token: symbol) {
      retrying.delete(token)
      publish()
    },
    /** Busy was seen: other reads skip retrying while this chain backs off. */
    holdOff(token: symbol) {
      holders.add(token)
    },
    releaseHold(token: symbol) {
      holders.delete(token)
    },
    gaveUp() {
      gaveUpLatched = true
      if (!dismissed) noticeShown = true
      publish()
    },
    recovered() {
      gaveUpLatched = false
      noticeShown = false
      dismissed = false
      publish()
    },
    /** Hides the notice for the rest of this busy episode; retries stay held off. */
    dismiss() {
      dismissed = true
      noticeShown = false
      publish()
    },
  }
}

export type BusyTracker = ReturnType<typeof createBusyTracker>

export const busyTracker = createBusyTracker()

function abortReason(signal?: AbortSignal | null): unknown {
  return signal?.reason ?? new DOMException("The operation was aborted.", "AbortError")
}

function sleep(ms: number, signal?: AbortSignal | null): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortReason(signal))
      return
    }
    const onAbort = () => {
      clearTimeout(timer)
      reject(abortReason(signal))
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
 *
 * While another read is backing off from busy, or once a read has given up
 * (until one succeeds), later reads fail at once: overlapping polls must not
 * multiply load on a server that is full.
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
  const maxRetries = autoRetry && !tracker.isHoldingOff() ? BUSY_MAX_AUTO_RETRIES : 0
  try {
    for (let retries = 0; ; retries += 1) {
      const res = await send()
      if (!(await isBusyResponse(res))) {
        // Only a real answer proves capacity is back; a 5xx or a 401 says nothing.
        if (res.ok) tracker.recovered()
        return res
      }
      if (retries >= maxRetries) {
        // A write never retries, so its busy answer is its caller's to show.
        if (autoRetry) tracker.gaveUp()
        return res
      }
      tracker.holdOff(token)
      tracker.startRetry(token)
      await sleep(busyRetryDelayMs(res.headers.get("Retry-After"), retries + 1), signal)
    }
  } finally {
    tracker.releaseHold(token)
    tracker.settle(token)
  }
}

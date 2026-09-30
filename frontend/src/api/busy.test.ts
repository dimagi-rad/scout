import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { api, ApiError } from "./client"
import {
  BUSY_MAX_AUTO_RETRIES,
  busyRetryDelayMs,
  busyTracker,
  createBusyTracker,
  fetchWithBusyRetry,
  isBusyBody,
} from "./busy"

const BUSY = { error: "busy", message: "Scout is busy right now. Please try again in a few seconds." }

function busyResponse(retryAfter: string | null = "5") {
  return new Response(JSON.stringify(BUSY), {
    status: 503,
    headers: {
      "Content-Type": "application/json",
      ...(retryAfter === null ? {} : { "Retry-After": retryAfter }),
    },
  })
}

function okResponse(body: unknown = { ok: true }) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  busyTracker.dismissAll()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("busy answers", () => {
  it("recognises only the busy body", () => {
    expect(isBusyBody(BUSY)).toBe(true)
    expect(isBusyBody({ error: "Something failed" })).toBe(false)
    expect(isBusyBody(undefined)).toBe(false)
  })

  it("honours Retry-After and otherwise backs off exponentially", () => {
    expect(busyRetryDelayMs("5", 1)).toBe(5000)
    expect(busyRetryDelayMs(2, 3)).toBe(2000)
    expect(busyRetryDelayMs(null, 1)).toBe(1000)
    expect(busyRetryDelayMs(null, 3)).toBe(4000)
    expect(busyRetryDelayMs("3600", 1)).toBe(30_000)
  })
})

describe("fetchWithBusyRetry", () => {
  it("retries after the server's Retry-After and shows a retrying notice meanwhile", async () => {
    const tracker = createBusyTracker()
    const send = vi.fn()
      .mockResolvedValueOnce(busyResponse("5"))
      .mockResolvedValueOnce(okResponse())

    const pending = fetchWithBusyRetry(send, { autoRetry: true, tracker })
    await vi.advanceTimersByTimeAsync(0)
    expect(tracker.getSnapshot()).toEqual({ retrying: 1, waiting: 0 })

    await vi.advanceTimersByTimeAsync(4999)
    expect(send).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)

    const res = await pending
    expect(res.status).toBe(200)
    expect(send).toHaveBeenCalledTimes(2)
    expect(tracker.getSnapshot()).toEqual({ retrying: 0, waiting: 0 })
  })

  it("stops after a few automatic tries and waits for a manual Retry", async () => {
    const tracker = createBusyTracker()
    const send = vi.fn().mockResolvedValue(busyResponse("1"))

    const pending = fetchWithBusyRetry(send, { autoRetry: true, tracker })
    await vi.advanceTimersByTimeAsync(60_000)

    expect(send).toHaveBeenCalledTimes(BUSY_MAX_AUTO_RETRIES + 1)
    expect(tracker.getSnapshot()).toEqual({ retrying: 0, waiting: 1 })

    send.mockResolvedValueOnce(okResponse())
    tracker.retryAll()
    const res = await pending
    expect(res.status).toBe(200)
    expect(send).toHaveBeenCalledTimes(BUSY_MAX_AUTO_RETRIES + 2)
    expect(tracker.getSnapshot()).toEqual({ retrying: 0, waiting: 0 })
  })

  it("never repeats a mutation on its own", async () => {
    const tracker = createBusyTracker()
    const send = vi.fn().mockResolvedValue(busyResponse())

    const pending = fetchWithBusyRetry(send, { autoRetry: false, tracker })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(send).toHaveBeenCalledTimes(1)
    expect(tracker.getSnapshot()).toEqual({ retrying: 0, waiting: 1 })

    tracker.dismissAll()
    expect((await pending).status).toBe(503)
  })

  it("passes other failures straight through", async () => {
    const tracker = createBusyTracker()
    const unavailable = new Response(JSON.stringify({ error: "Cube is down" }), { status: 503 })
    const send = vi.fn().mockResolvedValue(unavailable)

    expect(await fetchWithBusyRetry(send, { autoRetry: true, tracker })).toBe(unavailable)
    expect(send).toHaveBeenCalledTimes(1)
  })
})

describe("api client", () => {
  it("retries a busy read transparently", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(busyResponse("1"))
      .mockResolvedValueOnce(okResponse({ value: 42 }))
    vi.stubGlobal("fetch", fetchMock)

    const pending = api.get<{ value: number }>("/api/things/")
    await vi.advanceTimersByTimeAsync(1000)
    await expect(pending).resolves.toEqual({ value: 42 })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it("reports a dismissed busy mutation as a calm ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(busyResponse()))

    const pending = api.post("/api/things/", { a: 1 })
    await vi.advanceTimersByTimeAsync(0)
    expect(busyTracker.getSnapshot().waiting).toBe(1)
    busyTracker.dismissAll()

    const error = await pending.catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 503, message: BUSY.message })
  })
})

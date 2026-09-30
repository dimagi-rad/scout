import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { api, ApiError } from "./client"
import {
  BUSY_MAX_AUTO_RETRIES,
  BUSY_MESSAGE,
  busyRetryDelayMs,
  busyTracker,
  createBusyTracker,
  fetchWithBusyRetry,
  isBusyBody,
} from "./busy"

const BUSY = { error: "busy", code: "CAPACITY_EXHAUSTED", message: BUSY_MESSAGE }
const noJitter = () => 0

function busyResponse(retryAfter: string | null = "5", body: unknown = BUSY) {
  return new Response(JSON.stringify(body), {
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

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(Math, "random").mockReturnValue(0)
})
afterEach(() => {
  busyTracker.recovered()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("busy answers", () => {
  it("recognises only the busy body", () => {
    expect(isBusyBody(BUSY)).toBe(true)
    expect(isBusyBody({ error: "Something failed" })).toBe(false)
    expect(isBusyBody(undefined)).toBe(false)
  })

  it("honours Retry-After and otherwise backs off exponentially", () => {
    expect(busyRetryDelayMs("5", 1, noJitter)).toBe(5000)
    expect(busyRetryDelayMs(2, 3, noJitter)).toBe(2000)
    expect(busyRetryDelayMs(null, 1, noJitter)).toBe(1000)
    expect(busyRetryDelayMs(null, 3, noJitter)).toBe(4000)
    expect(busyRetryDelayMs(null, 10, noJitter)).toBe(30_000)
  })

  it("spreads clients out without ever retrying before the server asked", () => {
    expect(busyRetryDelayMs("5", 1, () => 0)).toBeCloseTo(5000)
    expect(busyRetryDelayMs("5", 1, () => 1)).toBeCloseTo(7000)
    expect(busyRetryDelayMs("0", 1, () => 0)).toBe(500)
    expect(busyRetryDelayMs("60", 1, () => 0)).toBe(60_000)
    expect(busyRetryDelayMs("3600", 1, () => 0)).toBe(60_000)
  })

  it("keeps a proxy's huge Retry-After from hanging a request for hours", () => {
    expect(busyRetryDelayMs("50", 1, () => 1)).toBe(60_000)
    expect(busyRetryDelayMs(null, 10, () => 1)).toBe(30_000)
  })
})

describe("fetchWithBusyRetry", () => {
  it("retries after the server's Retry-After and says so meanwhile", async () => {
    const tracker = createBusyTracker()
    const send = vi.fn()
      .mockResolvedValueOnce(busyResponse("5"))
      .mockResolvedValueOnce(okResponse())

    const pending = fetchWithBusyRetry(send, { autoRetry: true, tracker })
    await vi.advanceTimersByTimeAsync(0)
    expect(tracker.getSnapshot()).toEqual({ retrying: 1, stillBusy: false })

    await vi.advanceTimersByTimeAsync(4999)
    expect(send).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)

    expect((await pending).status).toBe(200)
    expect(send).toHaveBeenCalledTimes(2)
    expect(tracker.getSnapshot()).toEqual({ retrying: 0, stillBusy: false })
  })

  it("gives up after a few automatic tries and hands back the busy answer", async () => {
    const tracker = createBusyTracker()
    const send = vi.fn().mockResolvedValue(busyResponse("1"))

    const pending = fetchWithBusyRetry(send, { autoRetry: true, tracker })
    await vi.advanceTimersByTimeAsync(60_000)

    expect((await pending).status).toBe(503)
    expect(send).toHaveBeenCalledTimes(BUSY_MAX_AUTO_RETRIES + 1)
    expect(tracker.getSnapshot()).toEqual({ retrying: 0, stillBusy: true })
  })

  it("never repeats a mutation on its own", async () => {
    const tracker = createBusyTracker()
    const send = vi.fn().mockResolvedValue(busyResponse())

    expect((await fetchWithBusyRetry(send, { autoRetry: false, tracker })).status).toBe(503)
    expect(send).toHaveBeenCalledTimes(1)
    // The write's caller shows busy itself; the global notice is for reads that gave up.
    expect(tracker.getSnapshot().stillBusy).toBe(false)
  })

  it("stops retrying reads while an earlier read has already given up", async () => {
    const tracker = createBusyTracker()
    tracker.gaveUp()
    const send = vi.fn().mockResolvedValue(busyResponse("1"))

    expect((await fetchWithBusyRetry(send, { autoRetry: true, tracker })).status).toBe(503)
    expect(send).toHaveBeenCalledTimes(1)
  })

  it("keeps holding off after a failure that proves nothing about capacity", async () => {
    const tracker = createBusyTracker()
    tracker.gaveUp()
    const failed = new Response(JSON.stringify({ error: "Server error" }), { status: 500 })
    await fetchWithBusyRetry(vi.fn().mockResolvedValue(failed), { autoRetry: true, tracker })
    expect(tracker.getSnapshot().stillBusy).toBe(true)
    expect(tracker.isHoldingOff()).toBe(true)
  })

  it("dismissing the notice hides it for the episode without re-arming retries", async () => {
    const tracker = createBusyTracker()
    tracker.gaveUp()
    tracker.dismiss()
    expect(tracker.getSnapshot().stillBusy).toBe(false)

    const send = vi.fn().mockResolvedValue(busyResponse("1"))
    await fetchWithBusyRetry(send, { autoRetry: true, tracker })
    expect(send).toHaveBeenCalledTimes(1)
    // The next background poll that gives up must not bring the notice back.
    expect(tracker.getSnapshot().stillBusy).toBe(false)

    await fetchWithBusyRetry(vi.fn().mockResolvedValue(okResponse()), { autoRetry: true, tracker })
    tracker.gaveUp()
    expect(tracker.getSnapshot().stillBusy).toBe(true)
  })

  it.each([
    ["an abort during the backoff", "abort"],
    ["a non-busy failure on the retry", "error"],
  ])("releases the hold when a chain ends by %s", async (_label, ending) => {
    const tracker = createBusyTracker()
    const controller = new AbortController()
    const failed = new Response(JSON.stringify({ error: "Server error" }), { status: 500 })
    const send = vi.fn()
      .mockResolvedValueOnce(busyResponse("5"))
      .mockResolvedValueOnce(failed)

    const chain = fetchWithBusyRetry(send, { autoRetry: true, signal: controller.signal, tracker })
      .catch((error: unknown) => error)
    await vi.advanceTimersByTimeAsync(0)
    expect(tracker.isHoldingOff()).toBe(true)

    if (ending === "abort") controller.abort()
    else await vi.advanceTimersByTimeAsync(10_000)
    await chain

    expect(tracker.isHoldingOff()).toBe(false)
    expect(tracker.getSnapshot()).toEqual({ retrying: 0, stillBusy: false })
  })

  it("holds other reads off as soon as one sees busy", async () => {
    const tracker = createBusyTracker()
    const first = fetchWithBusyRetry(vi.fn().mockResolvedValue(busyResponse("5")), {
      autoRetry: true,
      tracker,
    })
    await vi.advanceTimersByTimeAsync(0)
    const second = vi.fn().mockResolvedValue(busyResponse("5"))
    await fetchWithBusyRetry(second, { autoRetry: true, tracker })
    expect(second).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(60_000)
    await first
  })

  it("stops backing off when the caller aborts", async () => {
    const tracker = createBusyTracker()
    const controller = new AbortController()
    const send = vi.fn().mockResolvedValue(busyResponse("5"))

    const pending = fetchWithBusyRetry(send, { autoRetry: true, signal: controller.signal, tracker })
    const outcome = pending.catch((error: unknown) => error)
    await vi.advanceTimersByTimeAsync(0)
    controller.abort()

    expect(await outcome).toMatchObject({ name: "AbortError" })
    expect(send).toHaveBeenCalledTimes(1)
    expect(tracker.getSnapshot()).toEqual({ retrying: 0, stillBusy: false })
  })

  it("clears the still-busy notice once a request succeeds again", async () => {
    const tracker = createBusyTracker()
    tracker.gaveUp()
    await fetchWithBusyRetry(vi.fn().mockResolvedValue(okResponse()), { autoRetry: true, tracker })
    expect(tracker.getSnapshot().stillBusy).toBe(false)
  })

  it("passes other failures straight through", async () => {
    const tracker = createBusyTracker()
    const unavailable = new Response(JSON.stringify({ error: "Cube is down" }), { status: 503 })
    const send = vi.fn().mockResolvedValue(unavailable)

    expect(await fetchWithBusyRetry(send, { autoRetry: true, tracker })).toBe(unavailable)
    expect(send).toHaveBeenCalledTimes(1)
    expect(tracker.getSnapshot().stillBusy).toBe(false)
  })
})

describe("api client", () => {
  it("retries a busy read transparently", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(busyResponse("1"))
      .mockResolvedValueOnce(okResponse({ value: 42 }))
    vi.stubGlobal("fetch", fetchMock)

    const pending = api.get<{ value: number }>("/api/things/")
    await vi.advanceTimersByTimeAsync(1200)
    await expect(pending).resolves.toEqual({ value: 42 })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it("fails a busy mutation at once with the friendly message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(busyResponse()))

    const error = await api.post("/api/things/", { a: 1 }).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 503, message: BUSY_MESSAGE })
  })

  it("never shows the machine code when a busy body has no message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(busyResponse(null, { error: "busy" })))

    const error = await api.post("/api/things/", {}).catch((e: unknown) => e)
    expect(error).toMatchObject({ status: 503, message: BUSY_MESSAGE })
  })
})

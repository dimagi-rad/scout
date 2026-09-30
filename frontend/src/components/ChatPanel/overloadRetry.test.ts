import { describe, expect, it } from "vitest"
import {
  busyRetryAfter,
  decideOverloadAction,
  isBusyChatError,
  isRetryableErrorPart,
} from "./overloadRetry"

describe("decideOverloadAction", () => {
  it("does nothing when no retryable error occurred", () => {
    expect(decideOverloadAction({ hitRetryable: false, alreadyRetried: false })).toBe("none")
    expect(decideOverloadAction({ hitRetryable: false, alreadyRetried: true })).toBe("none")
  })

  it("auto-retries once on the first retryable error", () => {
    expect(decideOverloadAction({ hitRetryable: true, alreadyRetried: false })).toBe("retry")
  })

  it("notifies instead of retrying again after one retry", () => {
    expect(decideOverloadAction({ hitRetryable: true, alreadyRetried: true })).toBe("notify")
  })
})

describe("isRetryableErrorPart", () => {
  it("matches the backend retryable-error data part", () => {
    expect(
      isRetryableErrorPart({
        type: "data-chat-status",
        data: { kind: "retryable-error", reason: "overloaded" },
      }),
    ).toBe(true)
  })

  it("ignores other kinds, types, and malformed shapes", () => {
    expect(isRetryableErrorPart({ type: "data-chat-status", data: { kind: "other" } })).toBe(false)
    expect(
      isRetryableErrorPart({ type: "text-delta", data: { kind: "retryable-error" } }),
    ).toBe(false)
    expect(isRetryableErrorPart({ type: "data-chat-status" })).toBe(false)
    expect(isRetryableErrorPart({ type: "data-chat-status", data: null })).toBe(false)
    expect(isRetryableErrorPart({})).toBe(false)
  })
})

describe("busy chat turns", () => {
  const busyPart = (data: Record<string, unknown>) => ({
    type: "data-chat-status",
    data: { kind: "retryable-error", reason: "busy", ...data },
  })

  it("reads the backoff hint from the backend's busy part", () => {
    expect(busyRetryAfter(busyPart({ retryAfter: 5 }))).toBe(5)
    expect(busyRetryAfter(busyPart({}))).toBeNull()
  })

  it("leaves the Anthropic overload signal to its own handling", () => {
    expect(
      busyRetryAfter({ type: "data-chat-status", data: { kind: "retryable-error", reason: "overloaded" } }),
    ).toBeUndefined()
    expect(busyRetryAfter({ type: "text-delta" })).toBeUndefined()
  })

  it("recognises a chat POST answered busy", () => {
    const body = { error: "busy", code: "CAPACITY_EXHAUSTED", message: "Scout is busy." }
    expect(isBusyChatError(new Error(JSON.stringify(body)))).toBe(true)
    expect(isBusyChatError(new Error('{"error": "Thread not found"}'))).toBe(false)
    expect(isBusyChatError(new Error("Failed to fetch"))).toBe(false)
  })
})

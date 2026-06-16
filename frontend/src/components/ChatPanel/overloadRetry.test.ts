import { describe, expect, it } from "vitest"
import { decideOverloadAction, isRetryableErrorPart } from "./overloadRetry"

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

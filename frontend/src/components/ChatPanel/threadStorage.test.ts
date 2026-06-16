import { beforeEach, describe, expect, it } from "vitest"
import {
  clearSavedThreadId,
  readSavedThreadId,
  writeSavedThreadId,
} from "./threadStorage"

describe("threadStorage (per-workspace last thread)", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("round-trips a saved thread id per workspace", () => {
    writeSavedThreadId("ws-a", "thread-a")
    writeSavedThreadId("ws-b", "thread-b")
    expect(readSavedThreadId("ws-a")).toBe("thread-a")
    expect(readSavedThreadId("ws-b")).toBe("thread-b")
  })

  it("returns null when nothing is saved for a workspace", () => {
    expect(readSavedThreadId("ws-none")).toBeNull()
  })

  it("clearSavedThreadId removes the saved id when it matches", () => {
    writeSavedThreadId("ws-a", "thread-a")
    clearSavedThreadId("ws-a", "thread-a")
    expect(readSavedThreadId("ws-a")).toBeNull()
  })

  it("clearSavedThreadId does NOT clobber a newer saved thread (match guard)", () => {
    // A stale thread's failed load must not wipe a thread saved more recently.
    writeSavedThreadId("ws-a", "thread-new")
    clearSavedThreadId("ws-a", "thread-stale")
    expect(readSavedThreadId("ws-a")).toBe("thread-new")
  })

  it("clearSavedThreadId with no id removes unconditionally", () => {
    writeSavedThreadId("ws-a", "thread-a")
    clearSavedThreadId("ws-a")
    expect(readSavedThreadId("ws-a")).toBeNull()
  })
})

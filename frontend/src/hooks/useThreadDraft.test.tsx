import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, renderHook } from "@testing-library/react"

import { DRAFT_DEBOUNCE_MS, useThreadDraft } from "./useThreadDraft"

describe("useThreadDraft", () => {
  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it("debounces writes to scout:draft:{ws}:{thread}", () => {
    const { result } = renderHook(() => useThreadDraft("ws", "t1"))
    act(() => result.current[1]("hel"))
    expect(localStorage.getItem("scout:draft:ws:t1")).toBeNull()
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS)
    })
    expect(localStorage.getItem("scout:draft:ws:t1")).toContain("hel")
  })

  it("keeps the draft across a thread switch and restores it on return", () => {
    const { result, rerender } = renderHook(
      ({ thread }) => useThreadDraft("ws", thread),
      { initialProps: { thread: "t1" } },
    )
    act(() => result.current[1]("draft for one"))

    rerender({ thread: "t2" })
    expect(result.current[0]).toBe("")

    rerender({ thread: "t1" })
    expect(result.current[0]).toBe("draft for one")
  })

  it("works for a brand-new client-side thread id", () => {
    const id = crypto.randomUUID()
    const first = renderHook(() => useThreadDraft("ws", id))
    act(() => first.result.current[1]("fresh thread draft"))
    first.unmount()
    const second = renderHook(() => useThreadDraft("ws", id))
    expect(second.result.current[0]).toBe("fresh thread draft")
  })

  it("clears the stored draft immediately when set empty (send)", () => {
    const { result } = renderHook(() => useThreadDraft("ws", "t1"))
    act(() => result.current[1]("to send"))
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS)
    })
    act(() => result.current[1](""))
    expect(localStorage.getItem("scout:draft:ws:t1")).toBeNull()
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS * 2)
    })
    expect(localStorage.getItem("scout:draft:ws:t1")).toBeNull()
    expect(result.current[0]).toBe("")
  })

  it("does not persist without a workspace or thread", () => {
    const { result } = renderHook(() => useThreadDraft(null, null))
    act(() => result.current[1]("x"))
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS)
    })
    expect(localStorage.length).toBe(0)
    expect(result.current[0]).toBe("x")
  })

  it("prunes stale drafts on mount", () => {
    localStorage.setItem(
      "scout:draft:ws:ancient",
      JSON.stringify({ text: "old", updatedAt: Date.now() - 31 * 24 * 60 * 60 * 1000 }),
    )
    renderHook(() => useThreadDraft("ws", "t1"))
    expect(localStorage.getItem("scout:draft:ws:ancient")).toBeNull()
  })
})

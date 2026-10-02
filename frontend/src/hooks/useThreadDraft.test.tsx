import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, renderHook } from "@testing-library/react"

import { clearAllDrafts } from "@/components/ChatPanel/draftStorage"
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
    expect(localStorage.getItem("scout:draft:ws:t2")).toBeNull()
    expect(localStorage.getItem("scout:draft:ws:t1")).toContain("draft for one")

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

  it("flushes a pending edit on pagehide", () => {
    const { result } = renderHook(() => useThreadDraft("ws", "t1"))
    act(() => result.current[1]("typed just now"))
    window.dispatchEvent(new Event("pagehide"))
    expect(localStorage.getItem("scout:draft:ws:t1")).toContain("typed just now")
  })

  it("keeps the same thread id separate per workspace", () => {
    const { result, rerender } = renderHook(
      ({ ws }) => useThreadDraft(ws, "t1"),
      { initialProps: { ws: "ws-a" } },
    )
    act(() => result.current[1]("in a"))
    rerender({ ws: "ws-b" })
    expect(result.current[0]).toBe("")
    rerender({ ws: "ws-a" })
    expect(result.current[0]).toBe("in a")
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

  it("does not resurrect a pending edit after drafts are cleared (logout)", () => {
    const { result, unmount } = renderHook(() => useThreadDraft("ws", "t1"))
    act(() => result.current[1]("typed before logout"))
    clearAllDrafts()
    unmount()
    window.dispatchEvent(new Event("pagehide"))
    act(() => {
      vi.advanceTimersByTime(DRAFT_DEBOUNCE_MS * 2)
    })
    expect(localStorage.getItem("scout:draft:ws:t1")).toBeNull()
  })
})

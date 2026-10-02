import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { useResumeStream } from "./useResumeStream"

type Chunk = { id: number; run: string; text: string; done: boolean }

function serve(...batches: Chunk[][]) {
  const spy = vi.spyOn(api, "get")
  for (const batch of batches) spy.mockResolvedValueOnce({ chunks: batch })
  spy.mockResolvedValue({ chunks: [] })
  return spy
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe("useResumeStream", () => {
  it("tails the answer being written, from where it last read", async () => {
    const spy = serve(
      [{ id: 1, run: "r", text: "There were ", done: false }],
      [{ id: 2, run: "r", text: "42 visits.", done: false }],
    )
    const { result } = renderHook(() => useResumeStream("ws", "t", true))

    await waitFor(() => expect(result.current.text).toBe("There were 42 visits."), {
      timeout: 3000,
    })
    expect(spy.mock.calls[1][0]).toContain("after=1")
  })

  it("skips an earlier answer already finished when it starts reading", async () => {
    serve([
      { id: 1, run: "old", text: "An earlier answer.", done: false },
      { id: 2, run: "old", text: "", done: true },
      { id: 3, run: "new", text: "Now", done: false },
    ])
    const { result } = renderHook(() => useResumeStream("ws", "t", true))

    await waitFor(() => expect(result.current.text).toBe("Now"))
  })

  it("does not poll while nothing is answering, and keeps the text until reset", async () => {
    const spy = serve([{ id: 1, run: "r", text: "Partial", done: false }])
    const { result, rerender } = renderHook(({ active }) => useResumeStream("ws", "t", active), {
      initialProps: { active: false },
    })
    expect(spy).not.toHaveBeenCalled()

    rerender({ active: true })
    await waitFor(() => expect(result.current.text).toBe("Partial"))
    rerender({ active: false })
    expect(result.current.text).toBe("Partial")

    act(() => result.current.reset())
    expect(result.current.text).toBe("")
  })

  it("starts over in another chat", async () => {
    serve([{ id: 1, run: "r", text: "Chat one", done: false }])
    const { result, rerender } = renderHook(({ thread }) => useResumeStream("ws", thread, true), {
      initialProps: { thread: "t1" },
    })
    await waitFor(() => expect(result.current.text).toBe("Chat one"))

    rerender({ thread: "t2" })

    expect(result.current.text).toBe("")
  })

  it("reads each resume afresh, so an earlier one's unread end is not shown as new", async () => {
    const spy = serve([{ id: 1, run: "r1", text: "First answer", done: false }])
    const { result, rerender } = renderHook(({ active }) => useResumeStream("ws", "t", active), {
      initialProps: { active: true },
    })
    await waitFor(() => expect(result.current.text).toBe("First answer"))
    rerender({ active: false })
    act(() => result.current.reset())

    spy.mockResolvedValueOnce({
      chunks: [{ id: 2, run: "r1", text: " ends here", done: true }],
    })
    const before = spy.mock.calls.length
    rerender({ active: true })
    await waitFor(() => expect(spy.mock.calls.length).toBe(before + 1))
    await act(async () => {})

    expect(result.current.text).toBe("")
  })
})

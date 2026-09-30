import { act, renderHook } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { useRefetchOnLoadEnd } from "./useRefetchOnLoadEnd"

function deferredFetcher() {
  const pending: Array<(value: string) => void> = []
  const fetcher = () =>
    new Promise<string>((resolve) => {
      pending.push(resolve)
    })
  return { fetcher, pending }
}

describe("useRefetchOnLoadEnd", () => {
  it("keeps the response already in flight when a load starts", async () => {
    const { fetcher, pending } = deferredFetcher()
    const { result, rerender } = renderHook(
      ({ loading }) => useRefetchOnLoadEnd(fetcher, "ws-1", loading),
      { initialProps: { loading: false } },
    )
    rerender({ loading: true })

    await act(async () => pending[0]("before load"))

    expect(result.current[0]).toBe("before load")
  })

  it("does not let an older response overwrite a newer one", async () => {
    const { fetcher, pending } = deferredFetcher()
    const { result, rerender } = renderHook(
      ({ loading }) => useRefetchOnLoadEnd(fetcher, "ws-1", loading),
      { initialProps: { loading: false } },
    )
    rerender({ loading: true })
    rerender({ loading: false })
    expect(pending).toHaveLength(2)

    await act(async () => pending[1]("after load"))
    await act(async () => pending[0]("before load"))

    expect(result.current[0]).toBe("after load")
  })

  it("never returns another workspace's data", async () => {
    const { fetcher, pending } = deferredFetcher()
    const { result, rerender } = renderHook(
      ({ workspaceId, loading }) => useRefetchOnLoadEnd(fetcher, workspaceId, loading),
      { initialProps: { workspaceId: "ws-1", loading: false } },
    )
    await act(async () => pending[0]("ws-1 data"))

    rerender({ workspaceId: "ws-2", loading: true })

    expect(result.current[0]).toBeNull()
  })

  it("fetches again on demand", async () => {
    const { fetcher, pending } = deferredFetcher()
    const { result } = renderHook(() => useRefetchOnLoadEnd(fetcher, "ws-1"))
    await act(async () => pending[0]("first"))

    act(() => result.current[1]())
    await act(async () => pending[1]("second"))

    expect(result.current[0]).toBe("second")
  })
})

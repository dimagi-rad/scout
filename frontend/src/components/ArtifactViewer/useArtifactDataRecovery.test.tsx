import { act, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import type { ArtifactDataRecoveryState } from "./types"
import { useArtifactDataRecovery } from "./useArtifactDataRecovery"

const recovering: ArtifactDataRecoveryState = {
  status: "recovering",
  recovery_action: "semantic_rebuild",
  physical_status: "active",
  semantic_status: "unavailable",
  message: "Rebuilding the data model",
  can_retry: false,
  recovery: null,
}
const ready: ArtifactDataRecoveryState = {
  ...recovering,
  status: "ready",
  recovery_action: null,
  semantic_status: "ready",
}

describe("useArtifactDataRecovery polling", () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it("keeps polling unchanged progress and transient failures until ready", async () => {
    vi.useFakeTimers()
    const get = vi.spyOn(api, "get")
      .mockResolvedValueOnce(recovering)
      .mockResolvedValueOnce(recovering)
      .mockRejectedValueOnce(new Error("Temporary network failure"))
      .mockResolvedValueOnce(ready)
    const { result, unmount } = renderHook(() => useArtifactDataRecovery("artifact", "workspace", true))

    await act(async () => {})
    expect(result.current.state?.status).toBe("recovering")
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(result.current.state?.status).toBe("recovering")
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(result.current.error).toBe("Temporary network failure")
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(result.current.state?.status).toBe("ready")
    expect(result.current.error).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000) })
    expect(get).toHaveBeenCalledTimes(4)
    unmount()
  })

  it("ignores a previous artifact's response after navigation", async () => {
    let finishOldRequest!: (state: ArtifactDataRecoveryState) => void
    vi.spyOn(api, "get")
      .mockReturnValueOnce(new Promise(resolve => { finishOldRequest = resolve }))
      .mockResolvedValueOnce(recovering)
    const { result, rerender, unmount } = renderHook(
      ({ id }) => useArtifactDataRecovery(id, "workspace", true),
      { initialProps: { id: "old-artifact" } },
    )

    rerender({ id: "new-artifact" })
    await act(async () => {})
    await act(async () => { finishOldRequest(ready) })
    expect(result.current.state?.status).toBe("recovering")
    unmount()
  })
})

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAppStore } from "@/store/store"
import { api } from "@/api/client"
import type { Thread } from "@/store/uiSlice"

function thread(id: string, title: string): Thread {
  return {
    id,
    title,
    title_is_custom: title !== "Untitled",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    is_shared: false,
    is_public: false,
    share_token: null,
    last_viewed_at: null,
  }
}

describe("uiSlice.fetchThreads — outage vs empty (07#7)", () => {
  beforeEach(() => {
    useAppStore.setState({ threads: [], threadsStatus: "idle" })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("marks threadsStatus 'error' on load failure instead of 'loaded' with []", async () => {
    vi.spyOn(api, "get").mockRejectedValue(new Error("503 Service Unavailable"))

    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    // The failure must be distinguishable from a genuinely-empty history: a
    // silent {threads:[], status:'loaded'} read as "all conversations deleted".
    expect(useAppStore.getState().threadsStatus).toBe("error")
  })

  it("keeps previously-loaded threads visible when a refetch fails", async () => {
    const existing = [thread("t1", "Existing chat")]
    useAppStore.setState({ threads: existing, threadsStatus: "loaded" })

    vi.spyOn(api, "get").mockRejectedValue(new Error("network blip"))
    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    expect(useAppStore.getState().threadsStatus).toBe("error")
    // The list is not wiped to empty during a transient blip.
    expect(useAppStore.getState().threads).toEqual(existing)
  })

  it("loads threads and marks 'loaded' on success", async () => {
    const fetched = [thread("t2", "Loaded chat")]
    vi.spyOn(api, "get").mockResolvedValue(fetched as never)

    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    expect(useAppStore.getState().threadsStatus).toBe("loaded")
    expect(useAppStore.getState().threads).toEqual(fetched)
  })
})

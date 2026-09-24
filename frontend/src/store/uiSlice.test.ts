import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAppStore } from "@/store/store"
import { ApiError, api } from "@/api/client"
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
    useAppStore.setState({ activeDomainId: "ws-1", threads: [], threadsStatus: "idle" })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("marks threadsStatus 'error' on load failure instead of 'loaded' with []", async () => {
    vi.spyOn(api, "get").mockRejectedValue(new Error("503 Service Unavailable"))

    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    // A silent {threads:[], status:'loaded'} reads as "all conversations deleted".
    expect(useAppStore.getState().threadsStatus).toBe("error")
  })

  it("keeps previously-loaded threads visible when a refetch fails", async () => {
    const existing = [thread("t1", "Existing chat")]
    useAppStore.setState({ threads: existing, threadsStatus: "loaded" })

    vi.spyOn(api, "get").mockRejectedValue(new Error("network blip"))
    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    expect(useAppStore.getState().threadsStatus).toBe("error")
    expect(useAppStore.getState().threads).toEqual(existing)
  })

  it("loads threads and marks 'loaded' on success", async () => {
    const fetched = [thread("t2", "Loaded chat")]
    vi.spyOn(api, "get").mockResolvedValue(fetched as never)

    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    expect(useAppStore.getState().threadsStatus).toBe("loaded")
    expect(useAppStore.getState().threads).toEqual(fetched)
    expect(useAppStore.getState().threadsAccessLostMessage).toBeNull()
  })

  it("surfaces the server message when upstream tenant access was lost", async () => {
    const message =
      "You no longer have access to skelly: " +
      "not connected to your account — connect that account (Settings → Connections) " +
      "if you disconnected it or have not connected it yet. If access was removed " +
      "or restricted at the provider, ask a provider admin to restore it; " +
      "reconnecting alone cannot restore those permissions. A workspace admin " +
      "can help remove a source you no longer need."
    vi.spyOn(api, "get").mockRejectedValue(
      new ApiError(403, message, {
        error: message,
        reason: "tenant_access_lost",
        lost_tenants: ["skelly"],
      }),
    )

    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    expect(useAppStore.getState().threadsStatus).toBe("error")
    expect(useAppStore.getState().threadsAccessLostMessage).toBe(message)
  })

  it("does not set an access-lost message for a generic outage", async () => {
    vi.spyOn(api, "get").mockRejectedValue(new Error("503 Service Unavailable"))

    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    expect(useAppStore.getState().threadsAccessLostMessage).toBeNull()
  })
})

describe("uiSlice upstream-verification denials", () => {
  beforeEach(() => {
    useAppStore.setState({ activeDomainId: "ws-1", threads: [], threadsStatus: "idle" })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  const unavailable = "We couldn't verify your access to this workspace right now. Please retry shortly."

  it("flags a temporary verification failure as retryable", async () => {
    vi.spyOn(api, "get").mockRejectedValue(
      new ApiError(403, unavailable, {
        error: unavailable,
        reason: "verification_unavailable",
        retryable: true,
      }),
    )

    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    expect(useAppStore.getState().threadsAccessLostMessage).toBe(unavailable)
    expect(useAppStore.getState().threadsAccessRetryable).toBe(true)
  })

  it("does not offer a verification retry for an expired sign-in", async () => {
    const expired = "Your sign-in for one of this workspace's sources has expired."
    vi.spyOn(api, "get").mockRejectedValue(
      new ApiError(403, expired, { error: expired, reason: "credential_expired", retryable: false }),
    )

    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    expect(useAppStore.getState().threadsAccessLostMessage).toBe(expired)
    expect(useAppStore.getState().threadsAccessRetryable).toBe(false)
  })

  it("retries verification then reloads threads from the server", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ has_access: true } as never)
    const fetched = [thread("t3", "Recovered chat")]
    vi.spyOn(api, "get").mockResolvedValue(fetched as never)
    useAppStore.setState({ threadsAccessRetryable: true, threadsAccessLostMessage: unavailable })

    await useAppStore.getState().uiActions.retryAccessVerification("ws-1")

    expect(post).toHaveBeenCalledWith("/api/workspaces/ws-1/access/verify/", {})
    expect(useAppStore.getState().threads).toEqual(fetched)
    expect(useAppStore.getState().threadsAccessRetryable).toBe(false)
    expect(useAppStore.getState().threadsAccessLostMessage).toBeNull()
  })

  it("keeps a failed retry's denial without a second recheck", async () => {
    vi.spyOn(api, "post").mockRejectedValue(
      new ApiError(403, unavailable, {
        error: unavailable,
        reason: "verification_unavailable",
        retryable: true,
      }),
    )
    const get = vi.spyOn(api, "get")

    await useAppStore.getState().uiActions.retryAccessVerification("ws-1")

    expect(get).not.toHaveBeenCalled()
    expect(useAppStore.getState().threadsAccessLostMessage).toBe(unavailable)
    expect(useAppStore.getState().threadsAccessRetryable).toBe(true)
  })

  it("offers a recheck after upstream access was removed", async () => {
    const lost = "Your access to one of this workspace's sources was removed upstream."
    vi.spyOn(api, "get").mockRejectedValue(
      new ApiError(403, lost, { error: lost, reason: "upstream_access_lost", retryable: false }),
    )

    await useAppStore.getState().uiActions.fetchThreads("ws-1")

    expect(useAppStore.getState().threadsAccessRetryable).toBe(true)
  })
})

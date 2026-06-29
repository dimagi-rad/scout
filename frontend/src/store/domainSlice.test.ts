import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAppStore } from "@/store/store"
import { api } from "@/api/client"

describe("domainSlice.setActiveDomain — threadId leak guard (00c423d)", () => {
  beforeEach(() => {
    useAppStore.setState({ activeDomainId: "ws-a", threadId: "thread-a" })
  })

  it("resets threadId to a fresh id when switching to a different workspace", () => {
    useAppStore.getState().domainActions.setActiveDomain("ws-b")
    const s = useAppStore.getState()
    expect(s.activeDomainId).toBe("ws-b")
    expect(s.threadId).not.toBe("thread-a")
    expect(s.threadId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
    )
  })

  it("keeps threadId when re-selecting the same workspace", () => {
    useAppStore.getState().domainActions.setActiveDomain("ws-a")
    expect(useAppStore.getState().activeDomainId).toBe("ws-a")
    expect(useAppStore.getState().threadId).toBe("thread-a")
  })
})

describe("domainSlice.ensureTenant — surfaces failure, not empty (07#6)", () => {
  beforeEach(() => {
    useAppStore.setState({ domainsStatus: "idle", domainsError: null, domains: [] })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("sets an error state when ensure-tenant fails (vs silent empty)", async () => {
    vi.spyOn(api, "post").mockRejectedValue(new Error("ensure 503"))

    await useAppStore.getState().domainActions.ensureTenant("ocs", "team-1")

    // A failed resolution must be distinguishable from "account has no opportunities".
    expect(useAppStore.getState().domainsStatus).toBe("error")
    expect(useAppStore.getState().domainsError).toBeTruthy()
  })

  it("does not flag an error on success", async () => {
    vi.spyOn(api, "post").mockResolvedValue({ workspace_id: "ws-x" } as never)
    // fetchDomains runs after a successful ensure — stub the list call too.
    const { workspaceApi } = await import("@/api/workspaces")
    vi.spyOn(workspaceApi, "list").mockResolvedValue([] as never)

    await useAppStore.getState().domainActions.ensureTenant("ocs", "team-1")

    expect(useAppStore.getState().domainsStatus).not.toBe("error")
    expect(useAppStore.getState().domainsError).toBeNull()
  })
})

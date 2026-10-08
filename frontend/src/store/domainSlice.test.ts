import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAppStore } from "@/store/store"
import { api } from "@/api/client"
import { workspaceApi, type WorkspaceListItem } from "@/api/workspaces"
import { getRecentWorkspaceIds } from "@/lib/recentWorkspaces"

describe("domainSlice.setActiveDomain — threadId leak guard (00c423d)", () => {
  beforeEach(() => {
    localStorage.clear()
    useAppStore.setState({ activeDomainId: "ws-a" })
    useAppStore.setState({ threadId: "thread-a" })
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

  it("records an activated workspace as the most recent", () => {
    useAppStore.getState().domainActions.setActiveDomain("ws-a")
    useAppStore.getState().domainActions.setActiveDomain("ws-b")
    useAppStore.getState().domainActions.setActiveDomain("ws-a")

    expect(getRecentWorkspaceIds()).toEqual(["ws-a", "ws-b"])
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

describe("domainSlice.fetchDomains — default pick skips lost-access workspaces", () => {
  beforeEach(() => {
    useAppStore.setState({ activeDomainId: null, domains: [], domainsStatus: "idle" })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  const ws = (id: string, has_access: boolean) => ({
    id,
    name: id,
    display_name: id,
    is_auto_created: false,
    role: "manage",
    tenants: [{ id: `t-${id}`, tenant_name: id, provider: "commcare" }],
    has_access,
    member_count: 1,
    schema_status: "available",
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
  })

  it("defaults to the first accessible workspace, not an orphan listed first", async () => {
    const { workspaceApi } = await import("@/api/workspaces")
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("skelly", false), ws("live", true)] as never)

    await useAppStore.getState().domainActions.fetchDomains()

    expect(useAppStore.getState().activeDomainId).toBe("live")
  })

  it("moves off an active workspace that was deleted (D2)", async () => {
    useAppStore.setState({ activeDomainId: "gone", domains: [ws("gone", true), ws("live", true)] as never })
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("live", true)] as never)

    await useAppStore.getState().domainActions.fetchDomains()

    expect(useAppStore.getState().activeDomainId).toBe("live")
  })

  it("keeps an active id the previous list never had, such as a deep link", async () => {
    useAppStore.setState({ activeDomainId: "linked", domains: [ws("live", true)] as never })
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("live", true)] as never)

    await useAppStore.getState().domainActions.fetchDomains()

    expect(useAppStore.getState().activeDomainId).toBe("linked")
  })

  it("falls back to the first workspace when none are accessible", async () => {
    const { workspaceApi } = await import("@/api/workspaces")
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("skelly", false)] as never)

    await useAppStore.getState().domainActions.fetchDomains()

    expect(useAppStore.getState().activeDomainId).toBe("skelly")
  })
})

describe("domainSlice — remembered workspace (#860)", () => {
  const ws = (id: string, has_access = true) => ({
    id,
    name: id,
    display_name: id,
    is_auto_created: false,
    role: "manage",
    tenants: [],
    has_access,
    member_count: 1,
    schema_status: "available",
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
  })
  const signedIn = (last_workspace_id: string | null) => ({
    id: "u1",
    email: "u@example.com",
    name: "U",
    is_staff: false,
    onboarding_complete: true,
    last_workspace_id,
  })

  beforeEach(() => {
    // A new identity rebuilds the slices, dropping the in-flight request tracker.
    useAppStore.setState({ user: null })
    localStorage.clear()
    useAppStore.setState({ activeDomainId: null, domains: [], domainsStatus: "idle" })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("loads the remembered workspace instead of the first one", async () => {
    useAppStore.setState({ user: signedIn("b") })
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("a"), ws("b")] as never)

    await useAppStore.getState().domainActions.fetchDomains()

    expect(useAppStore.getState().activeDomainId).toBe("b")
  })

  it("falls back to the default when the remembered workspace is not listed", async () => {
    useAppStore.setState({ user: signedIn("gone") })
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("a"), ws("b")] as never)

    await useAppStore.getState().domainActions.fetchDomains()

    expect(useAppStore.getState().activeDomainId).toBe("a")
  })

  it("falls back when the remembered workspace lost access", async () => {
    useAppStore.setState({ user: signedIn("b") })
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("a"), ws("b", false)] as never)

    await useAppStore.getState().domainActions.fetchDomains()

    expect(useAppStore.getState().activeDomainId).toBe("a")
  })

  it("lets a deep link win over the remembered workspace", async () => {
    useAppStore.setState({ user: signedIn("b") })
    useAppStore.setState({ activeDomainId: "a" })
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("a"), ws("b")] as never)

    await useAppStore.getState().domainActions.fetchDomains()

    expect(useAppStore.getState().activeDomainId).toBe("a")
  })

  it("tells the server when the user switches workspace", async () => {
    useAppStore.setState({ user: signedIn("a") })
    const post = vi.spyOn(api, "post").mockResolvedValue({ ok: true } as never)

    useAppStore.getState().domainActions.setActiveDomain("b")

    expect(post).toHaveBeenCalledWith("/api/auth/last-workspace/", { workspace_id: "b" })
    await Promise.resolve()
    expect(useAppStore.getState().user?.last_workspace_id).toBe("b")
  })

  it("does not repeat the call for the workspace already remembered", () => {
    useAppStore.setState({ user: signedIn("a") })
    const post = vi.spyOn(api, "post").mockResolvedValue({ ok: true } as never)

    useAppStore.getState().domainActions.setActiveDomain("a")

    expect(post).not.toHaveBeenCalled()
  })

  it("sends the switch back when the first switch is still in flight", async () => {
    useAppStore.setState({ user: signedIn("a") })
    let resolveB: () => void = () => undefined
    const post = vi
      .spyOn(api, "post")
      .mockImplementationOnce(() => new Promise((r) => { resolveB = () => r({ ok: true } as never) }))
      .mockResolvedValue({ ok: true } as never)

    useAppStore.getState().domainActions.setActiveDomain("b")
    useAppStore.getState().domainActions.setActiveDomain("a")
    resolveB()
    await Promise.resolve()
    await Promise.resolve()

    expect(post).toHaveBeenCalledTimes(2)
    expect(post).toHaveBeenLastCalledWith("/api/auth/last-workspace/", { workspace_id: "a" })
    expect(useAppStore.getState().user?.last_workspace_id).toBe("a")
  })

  it("survives the server rejecting the workspace", async () => {
    useAppStore.setState({ user: signedIn("a") })
    vi.spyOn(api, "post").mockRejectedValue(new Error("404"))

    useAppStore.getState().domainActions.setActiveDomain("not-mine")
    await Promise.resolve()

    expect(useAppStore.getState().activeDomainId).toBe("not-mine")
    expect(useAppStore.getState().user?.last_workspace_id).toBe("a")
  })
})

describe("domainSlice.revalidateDomains — silent background refresh (#355)", () => {
  const ws = (id: string): WorkspaceListItem => ({
    id,
    name: id,
    display_name: id,
    is_auto_created: false,
    role: "manage",
    tenants: [],
    has_access: true,
    member_count: 1,
    schema_status: "available",
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
  })

  beforeEach(() => {
    useAppStore.setState({ activeDomainId: "a", domains: [ws("a")], domainsStatus: "loaded", domainsError: null })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("adds new workspaces without ever passing through loading", async () => {
    let resolve!: (value: WorkspaceListItem[]) => void
    vi.spyOn(workspaceApi, "list").mockReturnValue(new Promise((r) => { resolve = r }))
    const statuses: string[] = []
    const unsubscribe = useAppStore.subscribe((s) => statuses.push(s.domainsStatus))

    const pending = useAppStore.getState().domainActions.revalidateDomains()
    expect(useAppStore.getState().domainsStatus).toBe("loaded")
    resolve([ws("new"), ws("a")])
    await pending
    unsubscribe()

    expect(useAppStore.getState().domains.map((d) => d.id)).toEqual(["new", "a"])
    expect(useAppStore.getState().activeDomainId).toBe("a")
    expect(statuses.every((s) => s === "loaded")).toBe(true)
  })

  it("moves to the default workspace when the active one disappears from the list", async () => {
    useAppStore.setState({ domains: [ws("a"), ws("b")] })
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("b")])

    await useAppStore.getState().domainActions.revalidateDomains()

    expect(useAppStore.getState().activeDomainId).toBe("b")
  })

  it("keeps an active id that was never in the list, such as a deep link being checked", async () => {
    useAppStore.setState({ activeDomainId: "linked" })
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("a"), ws("b")])

    await useAppStore.getState().domainActions.revalidateDomains()

    expect(useAppStore.getState().activeDomainId).toBe("linked")
  })

  it("keeps the same list object when nothing changed, so subscribers don't re-run", async () => {
    const before = useAppStore.getState().domains
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("a")])

    await useAppStore.getState().domainActions.revalidateDomains()

    expect(useAppStore.getState().domains).toBe(before)
  })

  it("keeps the current list and status when the refresh fails", async () => {
    const before = useAppStore.getState().domains
    vi.spyOn(workspaceApi, "list").mockRejectedValue(new Error("503"))

    await useAppStore.getState().domainActions.revalidateDomains()

    expect(useAppStore.getState().domains).toBe(before)
    expect(useAppStore.getState().domainsStatus).toBe("loaded")
    expect(useAppStore.getState().domainsError).toBeNull()
  })

  it("leaves an initial load alone", async () => {
    useAppStore.setState({ domainsStatus: "loading" })
    const list = vi.spyOn(workspaceApi, "list")

    await useAppStore.getState().domainActions.revalidateDomains()

    expect(list).not.toHaveBeenCalled()
  })

  it("shares one request between overlapping calls", async () => {
    const list = vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("a")])
    const actions = useAppStore.getState().domainActions

    await Promise.all([actions.revalidateDomains(), actions.revalidateDomains()])

    expect(list).toHaveBeenCalledOnce()
  })

  it("reports whether it fetched, so callers can tell a skip from a refresh (D1, D5)", async () => {
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("a")])
    const actions = useAppStore.getState().domainActions

    expect(await actions.revalidateDomains()).toBe("fetched")
    useAppStore.setState({ domainsStatus: "loading" })
    expect(await actions.revalidateDomains()).toBe("skipped")
  })

  it("reports a failed refresh as failed, not fetched", async () => {
    vi.spyOn(workspaceApi, "list").mockRejectedValue(new Error("503"))

    expect(await useAppStore.getState().domainActions.revalidateDomains()).toBe("failed")
  })

  it("makes a fresh request rather than joining one that started before it (D1)", async () => {
    let resolveOlder!: (value: WorkspaceListItem[]) => void
    const list = vi.spyOn(workspaceApi, "list")
      .mockReturnValueOnce(new Promise((r) => { resolveOlder = r }))
      .mockResolvedValueOnce([ws("granted"), ws("a")])
    const actions = useAppStore.getState().domainActions

    const older = actions.revalidateDomains()
    const fresh = actions.revalidateDomains({ fresh: true })
    resolveOlder([ws("a")])

    expect(await fresh).toBe("fetched")
    await older
    expect(list).toHaveBeenCalledTimes(2)
    expect(useAppStore.getState().domains.map((d) => d.id)).toEqual(["granted", "a"])
  })

  it("drops its result when a full fetch starts after it", async () => {
    let resolveStale!: (value: WorkspaceListItem[]) => void
    vi.spyOn(workspaceApi, "list")
      .mockReturnValueOnce(new Promise((r) => { resolveStale = r }))
      .mockResolvedValueOnce([ws("fresh")])
    const actions = useAppStore.getState().domainActions

    const stale = actions.revalidateDomains()
    await actions.fetchDomains()
    resolveStale([ws("stale")])
    // Its answer was thrown away, so callers mustn't act on it as the current list.
    expect(await stale).toBe("skipped")

    expect(useAppStore.getState().domains.map((d) => d.id)).toEqual(["fresh"])
  })
})

describe("domainSlice.addedDomainIds — workspaces someone added you to (#355)", () => {
  const ws = (id: string): WorkspaceListItem => ({
    id,
    name: id,
    display_name: id,
    is_auto_created: false,
    role: "read",
    tenants: [],
    has_access: true,
    member_count: 2,
    schema_status: "available",
    last_synced_at: null,
    created_at: "2026-01-01T00:00:00Z",
  })

  beforeEach(() => {
    useAppStore.setState({
      activeDomainId: "a",
      domains: [ws("a")],
      domainsStatus: "loaded",
      domainsError: null,
      addedDomainIds: [],
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("records workspaces a background refresh finds for the first time", async () => {
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("new"), ws("a")])

    await useAppStore.getState().domainActions.revalidateDomains()

    expect(useAppStore.getState().addedDomainIds).toEqual(["new"])
  })

  it("doesn't record the workspace you're already in, such as a deep link being checked", async () => {
    useAppStore.setState({ activeDomainId: "linked" })
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("a"), ws("linked")])

    await useAppStore.getState().domainActions.revalidateDomains()

    expect(useAppStore.getState().addedDomainIds).toEqual([])
  })

  it("doesn't record a workspace the same refresh makes active", async () => {
    useAppStore.setState({ activeDomainId: null, domains: [] })
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("first")])

    await useAppStore.getState().domainActions.revalidateDomains()

    expect(useAppStore.getState().activeDomainId).toBe("first")
    expect(useAppStore.getState().addedDomainIds).toEqual([])
  })

  it("doesn't record a workspace listed without upstream access", async () => {
    vi.spyOn(workspaceApi, "list").mockResolvedValue([{ ...ws("locked"), has_access: false }, ws("a")])

    await useAppStore.getState().domainActions.revalidateDomains()

    expect(useAppStore.getState().addedDomainIds).toEqual([])
  })

  it("forgets an added workspace that a later refresh no longer lists", async () => {
    vi.spyOn(workspaceApi, "list")
      .mockResolvedValueOnce([ws("x"), ws("a")])
      .mockResolvedValueOnce([ws("a")])
    const actions = useAppStore.getState().domainActions

    await actions.revalidateDomains()
    expect(useAppStore.getState().addedDomainIds).toEqual(["x"])
    await actions.revalidateDomains()
    expect(useAppStore.getState().addedDomainIds).toEqual([])
  })

  it("forgets an added workspace that loses upstream access before you open it", async () => {
    vi.spyOn(workspaceApi, "list")
      .mockResolvedValueOnce([ws("x"), ws("a")])
      .mockResolvedValueOnce([{ ...ws("x"), has_access: false }, ws("a")])
    const actions = useAppStore.getState().domainActions

    await actions.revalidateDomains()
    expect(useAppStore.getState().addedDomainIds).toEqual(["x"])
    await actions.revalidateDomains()
    expect(useAppStore.getState().addedDomainIds).toEqual([])
  })

  it("keeps the same added list when a refresh neither adds nor drops one", async () => {
    vi.spyOn(workspaceApi, "list")
      .mockResolvedValueOnce([ws("x"), ws("a")])
      .mockResolvedValueOnce([ws("x"), { ...ws("a"), member_count: 3 }])
    const actions = useAppStore.getState().domainActions

    await actions.revalidateDomains()
    const before = useAppStore.getState().addedDomainIds
    await actions.revalidateDomains()
    expect(useAppStore.getState().addedDomainIds).toBe(before)
  })

  it("doesn't record anything from a full fetch, such as after creating a workspace yourself", async () => {
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("mine"), ws("a")])

    await useAppStore.getState().domainActions.fetchDomains()

    expect(useAppStore.getState().addedDomainIds).toEqual([])
  })

  it("forgets an added workspace once it's dismissed or opened", async () => {
    vi.spyOn(workspaceApi, "list").mockResolvedValue([ws("x"), ws("y"), ws("a")])
    const actions = useAppStore.getState().domainActions
    await actions.revalidateDomains()

    actions.dismissAddedDomain("x")
    expect(useAppStore.getState().addedDomainIds).toEqual(["y"])

    actions.setActiveDomain("y")
    expect(useAppStore.getState().addedDomainIds).toEqual([])
  })
})

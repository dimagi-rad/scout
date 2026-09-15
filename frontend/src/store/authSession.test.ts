import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { api, ApiError } from "@/api/client"
import { workspaceApi } from "@/api/workspaces"
import { getRecentWorkspaceIds } from "@/lib/recentWorkspaces"
import { createAppStore, type AppStore } from "./store"
import type { User } from "./authSlice"
import type { TableAnnotations } from "./dictionarySlice"

const user = (id: string): User => ({
  id, email: `${id}@example.invalid`, name: id, is_staff: false, onboarding_complete: true,
})
const USER_A = user("user-a")
const USER_B = user("user-b")
const workspace = (id: string) => ({
  id, name: id, display_name: id, has_access: true, is_auto_created: false,
  role: "manage" as const, tenants: [], member_count: 1, schema_status: "available" as const,
  last_synced_at: null, created_at: "2026-01-01T00:00:00Z",
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function signedIn(account = USER_A) {
  const store = createAppStore()
  store.setState({ user: account, authStatus: "authenticated" })
  store.getState().domainActions.setActiveDomain("workspace-a")
  return store
}

function seedPrivateState(store: ReturnType<typeof createAppStore>, suffix = "a") {
  const id = `private-${suffix}`
  store.setState({
    domains: [workspace(id)], domainsStatus: "loaded", domainsError: id, activeDomainId: id,
    threadId: id, activeArtifactId: id,
    threads: [{ id, title: id } as AppStore["threads"][number]],
    threadsStatus: "loaded", threadsAccessLostMessage: id,
    artifacts: [{ id, title: id } as AppStore["artifacts"][number]],
    artifactsStatus: "loaded", artifactsError: id, artifactSearch: id,
    dataDictionary: { schemas: { [id]: {} } }, dictionaryStatus: "loaded", dictionaryError: id,
    selectedTable: { schema: id, table: id, columns: [], annotations: null, sourceMetadata: null },
    datasetCatalog: { datasets: [] } as unknown as AppStore["datasetCatalog"],
    datasetStatus: "loaded", datasetError: id,
    selectedDataset: { name: id } as AppStore["selectedDataset"],
    selectedDatasetStatus: "loaded", selectedDatasetError: id,
    knowledgeItems: [{ id, type: "entry", title: id, content: id, tags: [], created_at: "", updated_at: "" }],
    knowledgeStatus: "loaded", knowledgeError: id, knowledgeSearch: id,
    knowledgePagination: { count: 1 } as unknown as AppStore["knowledgePagination"],
    recipes: [{ id, name: id } as AppStore["recipes"][number]], recipeStatus: "loaded", recipeError: id,
    currentRecipe: { id, name: id } as AppStore["currentRecipe"],
    recipeRuns: [{ id } as AppStore["recipeRuns"][number]],
  })
}

async function loginAs(store: ReturnType<typeof createAppStore>, account: User) {
  await Promise.resolve()
  vi.mocked(api.get).mockResolvedValue({})
  vi.mocked(api.post).mockResolvedValue(account)
  await store.getState().authActions.login(account.email, "synthetic-password")
}

beforeEach(() => {
  localStorage.clear()
  vi.spyOn(api, "get")
  vi.spyOn(api, "post")
  vi.spyOn(console, "error").mockImplementation(() => undefined)
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("account-owned browser side effects", () => {
  function mockDownload() {
    const createObjectURL = vi.fn(() => "blob:synthetic-export")
    const revokeObjectURL = vi.fn()
    vi.stubGlobal("URL", class extends URL {
      static createObjectURL = createObjectURL
      static revokeObjectURL = revokeObjectURL
    })
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined)
    return { createObjectURL, revokeObjectURL, click }
  }

  it.each([USER_B, USER_A])("discards A's pending export after logout → login $id", async (account) => {
    const store = signedIn()
    const pending = deferred<Blob>()
    vi.spyOn(api, "getBlob").mockReturnValue(pending.promise)
    const download = mockDownload()
    const exporting = store.getState().knowledgeActions.exportKnowledge()
    vi.mocked(api.post).mockResolvedValue(undefined)
    await store.getState().authActions.logout()
    await loginAs(store, account)

    pending.resolve(new Blob(["Synthetic private knowledge from A"]))
    await exporting

    expect(download.createObjectURL).not.toHaveBeenCalled()
    expect(download.click).not.toHaveBeenCalled()
  })

  it("discards A's pending export after a direct identity change", async () => {
    const store = signedIn()
    const pending = deferred<Blob>()
    vi.spyOn(api, "getBlob").mockReturnValue(pending.promise)
    const download = mockDownload()
    const exporting = store.getState().knowledgeActions.exportKnowledge()
    store.setState({ user: USER_B })

    pending.resolve(new Blob(["Synthetic private knowledge from A"]))
    await exporting

    expect(download.createObjectURL).not.toHaveBeenCalled()
    expect(download.click).not.toHaveBeenCalled()
  })

  it("does not start an export through an old account's captured action", async () => {
    const store = signedIn()
    const exportKnowledge = store.getState().knowledgeActions.exportKnowledge
    const getBlob = vi.spyOn(api, "getBlob").mockResolvedValue(new Blob())
    const download = mockDownload()
    store.setState({ user: USER_B })

    await exportKnowledge()

    expect(getBlob).not.toHaveBeenCalled()
    expect(download.click).not.toHaveBeenCalled()
  })

  it("still downloads the current account's export and releases its object URL", async () => {
    const store = signedIn()
    const blob = new Blob(["Synthetic current knowledge"])
    vi.spyOn(api, "getBlob").mockResolvedValue(blob)
    const download = mockDownload()

    await store.getState().knowledgeActions.exportKnowledge()

    expect(download.createObjectURL).toHaveBeenCalledWith(blob)
    expect(download.click).toHaveBeenCalledOnce()
    expect(download.revokeObjectURL).toHaveBeenCalledWith("blob:synthetic-export")
    expect(document.querySelector("a[download]")).toBeNull()
  })

  it("does not record workspace use through a previous account's captured action", () => {
    const store = signedIn()
    const setActiveDomain = store.getState().domainActions.setActiveDomain
    store.setState({ user: USER_B })
    store.getState().domainActions.setActiveDomain("workspace-b")
    const before = getRecentWorkspaceIds()

    setActiveDomain("private-a-created")

    expect(getRecentWorkspaceIds()).toEqual(before)
    expect(store.getState().activeDomainId).toBe("workspace-b")
  })
})

describe("account-owned store isolation", () => {
  it("clears all private state immediately, before logout finishes", async () => {
    const store = signedIn()
    seedPrivateState(store)
    const logoutResponse = deferred<void>()
    vi.mocked(api.post).mockReturnValue(logoutResponse.promise)

    const logout = store.getState().authActions.logout()

    expect(store.getState()).toMatchObject({
      user: null, authStatus: "unauthenticated", activeDomainId: null,
      domains: [], domainsStatus: "idle", domainsError: null,
      activeArtifactId: null, threads: [], threadsStatus: "idle", threadsAccessLostMessage: null,
      artifacts: [], artifactsStatus: "idle", artifactsError: null, artifactSearch: "",
      dataDictionary: null, dictionaryStatus: "idle", dictionaryError: null, selectedTable: null,
      datasetCatalog: null, datasetStatus: "idle", datasetError: null,
      selectedDataset: null, selectedDatasetStatus: "idle", selectedDatasetError: null,
      knowledgeItems: [], knowledgeStatus: "idle", knowledgeError: null, knowledgePagination: null,
      knowledgeFilter: null, knowledgeSearch: "",
      recipes: [], recipeStatus: "idle", recipeError: null, currentRecipe: null, recipeRuns: [],
    })
    expect(JSON.stringify(store.getState())).not.toContain("private-a")
    logoutResponse.resolve()
    await logout
  })

  it("defaults to B's accessible workspace after A logs out and B logs in", async () => {
    const store = signedIn()
    seedPrivateState(store)
    vi.mocked(api.post).mockResolvedValue(undefined)
    await store.getState().authActions.logout()
    await loginAs(store, USER_B)
    vi.spyOn(workspaceApi, "list").mockResolvedValue([workspace("workspace-b")])

    await store.getState().domainActions.fetchDomains()

    expect(store.getState().activeDomainId).toBe("workspace-b")
    expect(store.getState().artifacts).toEqual([])
    expect(store.getState().user).toEqual(USER_B)
  })

  it("clears state when an identity refresh discovers a different account", async () => {
    const store = signedIn()
    seedPrivateState(store)
    vi.mocked(api.get).mockResolvedValueOnce({}).mockResolvedValueOnce(USER_B)

    await store.getState().authActions.fetchMe()

    expect(store.getState().user).toEqual(USER_B)
    expect(store.getState().activeDomainId).toBeNull()
    expect(JSON.stringify(store.getState())).not.toContain("private-a")
  })

  it("preserves state and action identities when refreshing the same account", async () => {
    const store = signedIn()
    seedPrivateState(store)
    const before = store.getState()
    vi.mocked(api.get).mockResolvedValueOnce({}).mockResolvedValueOnce({ ...USER_A, name: "Updated" })

    await store.getState().authActions.fetchMe()

    expect(store.getState().artifacts).toBe(before.artifacts)
    expect(store.getState().artifactActions).toBe(before.artifactActions)
    expect(store.getState().activeDomainId).toBe("private-a")
  })

  it("keeps private state cleared if the logout request fails", async () => {
    const store = signedIn()
    seedPrivateState(store)
    vi.mocked(api.post).mockRejectedValue(new Error("logout failed"))

    await expect(store.getState().authActions.logout()).rejects.toThrow("logout failed")

    expect(store.getState().user).toBeNull()
    expect(store.getState().artifacts).toEqual([])
  })
})

const reads = [
  { name: "artifacts", start: (s: AppStore) => s.artifactActions.fetchArtifacts(), response: { results: [] } },
  { name: "dictionary", start: (s: AppStore) => s.dictionaryActions.fetchDictionary(), response: { schemas: {} } },
  { name: "datasets", start: (s: AppStore) => s.datasetActions.fetchDatasets(), response: { datasets: [] } },
  { name: "knowledge", start: (s: AppStore) => s.knowledgeActions.fetchKnowledge(), response: { results: [], pagination: null } },
  { name: "recipes", start: (s: AppStore) => s.recipeActions.fetchRecipes(), response: [] },
  { name: "threads", start: (s: AppStore) => s.uiActions.fetchThreads("workspace-a"), response: [] },
]

describe("old-session response fencing", () => {
  it.each(reads)("ignores a late $name success after logout → login B", async ({ start, response }) => {
    const store = signedIn()
    const pending = deferred<unknown>()
    vi.mocked(api.get).mockReturnValueOnce(pending.promise)
    const request = start(store.getState())
    vi.mocked(api.post).mockResolvedValue(undefined)
    await store.getState().authActions.logout()
    await loginAs(store, USER_B)
    seedPrivateState(store, "b")
    const before = store.getState()

    pending.resolve(response)
    await request

    expect(store.getState()).toBe(before)
  })

  it.each(reads)("ignores a late $name error after logout → login B", async ({ start }) => {
    const store = signedIn()
    const pending = deferred<unknown>()
    vi.mocked(api.get).mockReturnValueOnce(pending.promise)
    const request = start(store.getState())
    await loginAs(store, USER_B)
    seedPrivateState(store, "b")
    const before = store.getState()

    pending.reject(new ApiError(403, "old account lost access", { reason: "tenant_access_lost" }))
    await request

    expect(store.getState()).toBe(before)
  })

  it.each(["success", "error"])("ignores late workspace discovery %s from A", async (result) => {
    const store = signedIn()
    const pending = deferred<ReturnType<typeof workspace>[]>()
    vi.spyOn(workspaceApi, "list").mockReturnValueOnce(pending.promise)
    const request = store.getState().domainActions.fetchDomains()
    await loginAs(store, USER_B)
    seedPrivateState(store, "b")
    const before = store.getState()

    if (result === "success") pending.resolve([workspace("workspace-a")])
    else pending.reject(new Error("A discovery failed"))
    await request

    expect(store.getState()).toBe(before)
  })

  it("does not revive responses when the same account logs out and back in", async () => {
    const store = signedIn()
    const pending = deferred<unknown>()
    vi.mocked(api.get).mockReturnValueOnce(pending.promise)
    const request = store.getState().artifactActions.fetchArtifacts()
    vi.mocked(api.post).mockResolvedValue(undefined)
    await store.getState().authActions.logout()
    await loginAs(store, USER_A)
    const before = store.getState()

    pending.resolve({ results: [{ id: "old-session-artifact" }] })
    await request

    expect(store.getState()).toBe(before)
  })

  it("keeps delayed thread-view follow-ups in the expired session", async () => {
    const store = signedIn()
    const pending = deferred<void>()
    vi.mocked(api.post).mockReturnValueOnce(pending.promise)
    const selection = store.getState().uiActions.selectThread("thread-a")
    await loginAs(store, USER_B)
    seedPrivateState(store, "b")
    const currentFetch = vi.spyOn(store.getState().uiActions, "fetchThreads")
    vi.mocked(api.get).mockResolvedValue([{ id: "thread-a" }])
    const before = store.getState()

    pending.resolve()
    await selection

    expect(currentFetch).not.toHaveBeenCalled()
    expect(store.getState()).toBe(before)
  })

  it("prevents a late mutation from modifying B's dictionary objects in place", async () => {
    const store = signedIn()
    const pending = deferred<unknown>()
    vi.spyOn(api, "put").mockReturnValueOnce(pending.promise)
    const update = store.getState().dictionaryActions.updateAnnotations("public", "items", {})
    await loginAs(store, USER_B)
    const annotations = { description: "B-owned" } as TableAnnotations
    const dictionary = { schemas: { public: { items: { annotations } } } } as unknown as NonNullable<AppStore["dataDictionary"]>
    store.setState({ dataDictionary: dictionary })

    pending.resolve({ description: "A-owned" })
    await update

    expect(dictionary.schemas.public.items.annotations).toBe(annotations)
  })

  it("does not invalidate another store instance's requests", async () => {
    const first = signedIn()
    const second = signedIn()
    const pending = deferred<unknown>()
    vi.mocked(api.get).mockReturnValueOnce(pending.promise)
    const request = second.getState().artifactActions.fetchArtifacts()
    vi.mocked(api.post).mockResolvedValue(undefined)
    await first.getState().authActions.logout()

    pending.resolve({ results: [{ id: "second-store" }] })
    await request

    expect(second.getState().artifacts).toEqual([{ id: "second-store" }])
  })

  it("preserves a completed mutation's return value without merging it into B", async () => {
    const store = signedIn()
    const pending = deferred<unknown>()
    vi.mocked(api.post).mockReturnValueOnce(pending.promise)
    const creation = store.getState().knowledgeActions.createKnowledge({ type: "entry", title: "A note" })
    await loginAs(store, USER_B)
    seedPrivateState(store, "b")
    const before = store.getState()
    const created = { id: "entry-a", type: "entry", title: "A note" }

    pending.resolve(created)

    await expect(creation).resolves.toEqual(created)
    expect(store.getState()).toBe(before)
  })
})

describe("authentication request ordering", () => {
  it("does not let a late identity response restore A after logout", async () => {
    const store = signedIn()
    const pending = deferred<User>()
    vi.mocked(api.get).mockResolvedValueOnce({}).mockReturnValueOnce(pending.promise)
    const refresh = store.getState().authActions.fetchMe()
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
    vi.mocked(api.post).mockResolvedValue(undefined)
    await store.getState().authActions.logout()

    pending.resolve(USER_A)
    await refresh

    expect(store.getState().user).toBeNull()
    expect(store.getState().authStatus).toBe("unauthenticated")
  })

  it.each(["success", "error"])("ignores a superseded identity %s after B logs in", async (result) => {
    const store = signedIn()
    const pending = deferred<User>()
    vi.mocked(api.get).mockResolvedValueOnce({}).mockReturnValueOnce(pending.promise)
    const refresh = store.getState().authActions.fetchMe()
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
    await loginAs(store, USER_B)

    if (result === "success") pending.resolve(USER_A)
    else pending.reject(new ApiError(401, "expired"))
    await refresh

    expect(store.getState().user).toEqual(USER_B)
    expect(store.getState().authStatus).toBe("authenticated")
  })

  it("does not apply an older logout response to B's newer login", async () => {
    const store = signedIn()
    const pending = deferred<void>()
    vi.mocked(api.post).mockReturnValueOnce(pending.promise)
    const logout = store.getState().authActions.logout()
    const login = loginAs(store, USER_B)
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/auth/logout/"))
    expect(api.get).not.toHaveBeenCalled()

    pending.resolve()
    await Promise.all([logout, login])

    expect(store.getState().user).toEqual(USER_B)
    expect(api.post).toHaveBeenLastCalledWith("/api/auth/login/", {
      email: USER_B.email, password: "synthetic-password",
    })
  })

  it("does not submit a superseded login after its CSRF request finishes", async () => {
    const store = createAppStore()
    const pending = deferred<unknown>()
    vi.mocked(api.get).mockReturnValueOnce(pending.promise)
    const login = store.getState().authActions.login(USER_A.email, "synthetic-password")
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(1))
    vi.mocked(api.post).mockResolvedValue(undefined)
    const logout = store.getState().authActions.logout()

    pending.resolve({})
    await Promise.all([login, logout])

    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.post).toHaveBeenCalledWith("/api/auth/logout/")
    expect(store.getState().user).toBeNull()
  })

  it("does not apply a login response that arrives after logout", async () => {
    const store = createAppStore()
    const pending = deferred<User>()
    vi.mocked(api.get).mockResolvedValue({})
    vi.mocked(api.post).mockReturnValueOnce(pending.promise)
    const login = store.getState().authActions.login(USER_A.email, "synthetic-password")
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledTimes(1))
    vi.mocked(api.post).mockResolvedValue(undefined)
    const logout = store.getState().authActions.logout()

    pending.resolve(USER_A)
    await Promise.all([login, logout])

    expect(store.getState().user).toBeNull()
  })

  it.each(["logout", "login"])("ignores background identity checks during an explicit %s", async (operation) => {
    const store = signedIn()
    const pending = deferred<unknown>()
    vi.mocked(api.get).mockResolvedValue({})
    vi.mocked(api.post).mockReturnValueOnce(pending.promise)
    const mutation = operation === "logout"
      ? store.getState().authActions.logout()
      : store.getState().authActions.login(USER_B.email, "synthetic-password")
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledTimes(1))
    vi.mocked(api.get).mockClear()

    await store.getState().authActions.fetchMe()

    expect(api.get).not.toHaveBeenCalled()
    pending.resolve(operation === "login" ? USER_B : undefined)
    await mutation
    expect(store.getState().user).toEqual(operation === "login" ? USER_B : null)
  })

  it("allows B's login after an earlier logout request fails", async () => {
    const store = signedIn()
    const pending = deferred<void>()
    vi.mocked(api.post).mockReturnValueOnce(pending.promise)
    const logout = store.getState().authActions.logout()
    const rejected = expect(logout).rejects.toThrow("logout failed")
    const login = loginAs(store, USER_B)
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/auth/logout/"))

    pending.reject(new Error("logout failed"))
    await rejected
    await login

    expect(store.getState().user).toEqual(USER_B)
  })
})

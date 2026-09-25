import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { api, ApiError } from "@/api/client"
import { workspaceApi } from "@/api/workspaces"
import { createAppStore } from "./store"

function deferred<T = unknown>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
let store = createAppStore()
const state = () => store.getState()
const switchTo = (id: string) => state().domainActions.setActiveDomain(id)
const artifact = (title: string) => ({ id: "same-id", title, description: "", artifact_type: "html" as const, version: 1, has_live_queries: false, created_at: "", updated_at: "" })
const table = (name: string) => ({ schema: "public", name, columns: [] })
const detail = { schema: "public", table: "users", columns: [], annotations: null, sourceMetadata: null }
const thread = (title: string) => ({ id: "same-id", title, title_is_custom: false, created_at: "", updated_at: "", is_shared: false, is_public: false, share_token: null, last_viewed_at: null })

beforeEach(() => {
  store = createAppStore()
  switchTo("a")
})
afterEach(() => vi.restoreAllMocks())

const lists = [
  { name: "artifacts", start: () => state().artifactActions.fetchArtifacts(), response: (label: string) => ({ results: [artifact(label)] }), read: () => ({ data: state().artifacts, status: state().artifactsStatus, error: state().artifactsError }) },
  { name: "dictionary", start: () => state().dictionaryActions.fetchDictionary(), response: (label: string) => ({ tables: { [label]: table(label) } }), read: () => ({ data: state().dataDictionary, status: state().dictionaryStatus, error: state().dictionaryError }) },
  { name: "threads", start: () => state().uiActions.fetchThreads(state().activeDomainId!), response: (label: string) => [thread(label)], read: () => ({ data: state().threads, status: state().threadsStatus, error: state().threadsAccessLostMessage }) },
]
for (const list of lists) {
  describe(list.name, () => {
    for (const scenario of ["switch", "ABA", "overlap"]) {
      for (const outcome of ["success", "error"]) {
        it(`ignores stale ${outcome} after ${scenario}`, async () => {
          const old = deferred()
          vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never).mockResolvedValue(list.response("fresh") as never)
          const pending = list.start()
          if (scenario !== "overlap") switchTo("b")
          if (scenario === "ABA") switchTo("a")
          await list.start()
          const fresh = list.read()
          if (outcome === "success") old.resolve(list.response("old"))
          else old.reject(new ApiError(503, "old error", { reason: "tenant_access_lost" }))
          await pending
          expect(list.read()).toEqual(fresh)
        })
      }
    }
    it("does not end the newer request's loading state", async () => {
      const old = deferred(), latest = deferred()
      vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never).mockImplementationOnce(() => latest.promise as never)
      const first = list.start(), second = list.start()
      old.resolve(list.response("old"))
      await first
      expect(list.read().status).toBe("loading")
      latest.resolve(list.response("new"))
      await second
    })
    it("clears loaded data on switch before a failing new load", async () => {
      vi.spyOn(api, "get").mockResolvedValueOnce(list.response("a") as never).mockRejectedValueOnce(new Error("b failed"))
      await list.start()
      switchTo("b")
      const empty = list.read().data
      expect(JSON.stringify(empty)).not.toContain("same-id")
      if (list.name === "dictionary") expect(empty).toBeNull()
      await list.start()
      expect(list.read().data).toEqual(empty)
      expect(list.read().status).toBe("error")
    })
  })
}

it("does not start obsolete thread fetches or write loading state", async () => {
  switchTo("b")
  const get = vi.spyOn(api, "get")
  await state().uiActions.fetchThreads("a")
  expect(get).not.toHaveBeenCalled()
  expect(state().threadsStatus).toBe("idle")
})

it("does not refresh threads after a stale mark-viewed completes", async () => {
  const viewed = deferred()
  vi.spyOn(api, "post").mockImplementationOnce(() => viewed.promise as never)
  const get = vi.spyOn(api, "get").mockResolvedValue([])
  const pending = state().uiActions.selectThread("old-thread")
  switchTo("b")
  viewed.resolve({})
  await pending
  expect(get).not.toHaveBeenCalled()
})

it("does not follow a stale schema refresh with a dictionary load", async () => {
  const refresh = deferred()
  vi.spyOn(api, "post").mockImplementationOnce(() => refresh.promise as never)
  const get = vi.spyOn(api, "get").mockResolvedValue({ tables: {} })
  const pending = state().dictionaryActions.refreshSchema()
  switchTo("b")
  refresh.resolve({})
  await pending
  expect(get).not.toHaveBeenCalled()
  expect(state().dictionaryStatus).toBe("idle")
})

it("refresh and fetch dictionary share latest-request ownership", async () => {
  const old = deferred()
  vi.spyOn(api, "post").mockImplementationOnce(() => old.promise as never)
  const get = vi.spyOn(api, "get").mockResolvedValue({ tables: { fresh: table("fresh") } })
  const pending = state().dictionaryActions.refreshSchema()
  await state().dictionaryActions.fetchDictionary()
  old.resolve({})
  await pending
  expect(get).toHaveBeenCalledTimes(1)
  expect(state().dataDictionary?.schemas.public.fresh).toBeDefined()
})

for (const scenario of ["switch", "overlap", "clear"]) {
  it(`ignores stale table details after ${scenario}`, async () => {
    const old = deferred()
    vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never).mockResolvedValue(table("new"))
    const pending = state().dictionaryActions.fetchTable("public", "old")
    if (scenario === "switch") switchTo("b")
    if (scenario === "clear") state().dictionaryActions.clearDictionary()
    else await state().dictionaryActions.fetchTable("public", "new")
    const expected = state().selectedTable
    old.resolve(table("old"))
    await pending
    expect(state().selectedTable).toEqual(expected)
  })
}

it("clearDictionary invalidates pending dictionary responses", async () => {
  const old = deferred()
  vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never)
  const pending = state().dictionaryActions.fetchDictionary()
  state().dictionaryActions.clearDictionary()
  old.resolve({ tables: {} })
  await pending
  expect(state().dataDictionary).toBeNull()
  expect(state().dictionaryStatus).toBe("idle")
})

for (const operation of ["artifact update", "artifact delete", "annotation", "thread title", "thread sharing"]) {
  it(`ignores stale ${operation} writes while preserving mutation completion`, async () => {
    const old = deferred()
    vi.spyOn(api, "patch").mockImplementation(() => old.promise as never)
    vi.spyOn(api, "delete").mockImplementation(() => old.promise as never)
    vi.spyOn(api, "put").mockImplementation(() => old.promise as never)
    const pending = operation === "artifact update" ? state().artifactActions.updateArtifact("same-id", { title: "old" })
      : operation === "artifact delete" ? state().artifactActions.deleteArtifact("same-id")
      : operation === "annotation" ? state().dictionaryActions.updateAnnotations("public", "users", { description: "old" })
      : operation === "thread title" ? state().uiActions.updateThreadTitle("same-id", "old", "a")
      : state().uiActions.updateThreadSharing("same-id", { is_shared: true }, "a")
    switchTo("b")
    store.setState({ artifacts: [artifact("b")], threads: [thread("b")], selectedTable: detail, dataDictionary: { schemas: { public: { users: { columns: [] } } } } })
    const expected = { artifacts: state().artifacts, threads: state().threads, selectedTable: state().selectedTable, dataDictionary: structuredClone(state().dataDictionary) }
    const response = { ...thread("old"), description: "old", is_shared: true }
    old.resolve(response)
    const result = await pending
    expect({ artifacts: state().artifacts, threads: state().threads, selectedTable: state().selectedTable, dataDictionary: state().dataDictionary }).toEqual(expected)
    expect(result).toEqual(operation.startsWith("thread") ? response : undefined)
  })
}

it("invalidates pending reads when ensureTenant changes the workspace", async () => {
  const old = deferred()
  vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never)
  vi.spyOn(api, "post").mockResolvedValue({ workspace_id: "b" })
  vi.spyOn(workspaceApi, "list").mockResolvedValue([])
  const pending = state().artifactActions.fetchArtifacts()
  await state().domainActions.ensureTenant("commcare", "b")
  old.resolve({ results: [artifact("a")] })
  await pending
  expect(state().artifacts).toEqual([])
})

it("rejects an old visit after fetchDomains restores the same workspace", async () => {
  const old = deferred()
  vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never)
  vi.spyOn(workspaceApi, "list").mockResolvedValue([{ id: "a" }] as never)
  const pending = state().artifactActions.fetchArtifacts()
  store.setState({ activeDomainId: null })
  await state().domainActions.fetchDomains()
  expect(state().activeDomainId).toBe("a")
  old.resolve({ results: [artifact("old visit")] })
  await pending
  expect(state().artifacts).toEqual([])
})

it("keeps request ownership independent between stores", async () => {
  const other = createAppStore()
  other.getState().domainActions.setActiveDomain("a")
  const first = deferred()
  vi.spyOn(api, "get").mockImplementationOnce(() => first.promise as never).mockResolvedValue({ results: [artifact("other")] })
  const pending = state().artifactActions.fetchArtifacts()
  await other.getState().artifactActions.fetchArtifacts()
  first.resolve({ results: [artifact("first")] })
  await pending
  expect(state().artifacts[0].title).toBe("first")
  expect(other.getState().artifacts[0].title).toBe("other")
})

it("starts a fresh thread when tenant resolution changes the workspace", async () => {
  store.setState({ threadId: "a-thread" })
  vi.spyOn(api, "post").mockResolvedValue({ workspace_id: "b" })
  vi.spyOn(workspaceApi, "list").mockResolvedValue([])
  await state().domainActions.ensureTenant("commcare", "b")
  expect(state().activeDomainId).toBe("b")
  expect(state().threadId).not.toBe("a-thread")
})

it("a missing-workspace detail call leaves no orphaned loading state", async () => {
  const old = deferred()
  vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never)
  const pending = state().datasetActions.fetchDataset("records")
  expect(state().selectedDatasetStatus).toBe("loading")
  store.setState({ activeDomainId: null })
  await expect(state().datasetActions.fetchDataset("records")).rejects.toThrow("No active workspace")
  await expect(state().dictionaryActions.fetchTable("public", "records")).rejects.toThrow("No active domain")
  old.resolve({ dataset: { name: "records" } })
  await pending
  expect(state().selectedDatasetStatus).toBe("idle")
  expect(state().selectedDataset).toBeNull()
})

it("keeps the dictionary usable while surfacing a partial refresh warning", async () => {
  vi.spyOn(api, "post").mockResolvedValue({ status: "partial", error: "Some sources could not be refreshed: source B needs operator recovery." })
  const get = vi.spyOn(api, "get").mockResolvedValue({ tables: {} })
  await state().dictionaryActions.refreshSchema()
  expect(state().dictionaryStatus).toBe("loaded")
  expect(state().dictionaryError).toContain("source B needs operator recovery")
  expect(state().dataDictionary).not.toBeNull()
  expect(get).toHaveBeenCalledTimes(1)
})

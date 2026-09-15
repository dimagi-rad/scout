import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { api, ApiError } from "@/api/client"
import { createAppStore, type AppStore } from "./store"
import type { SemanticDataset } from "./datasetSlice"
import type { KnowledgeEntryItem } from "./knowledgeSlice"
import type { Recipe, RecipeRun } from "./recipeSlice"

function deferred<T = unknown>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
const dataset = (label: string): SemanticDataset => ({
  id: "same-id", name: "records", label, description: "", source_kind: "physical",
  definition_sql: "", schema_name: "public", table_name: "records", primary_key: "id",
  row_count: null, row_count_verified: false, dimensions: [], time_dimensions: [],
  measures: [], relationships: [], metadata: {},
})
const model = { id: "model", name: "model", version: 1, status: "ready", diagnostics: [], updated_at: "" }
const catalog = (label: string) => ({ model, datasets: [dataset(label)] })
const recipe = (name: string): Recipe => ({
  id: "same-id", name, description: "", prompt: "", variables: [], is_shared: false,
  created_at: "", updated_at: "",
})
const run = (label: string): RecipeRun => ({
  id: "same-id", status: "completed", variable_values: { label }, step_results: [],
  is_shared: false, is_public: false, share_token: null, started_at: null,
  completed_at: null, created_at: "",
})
const entry = (title: string): KnowledgeEntryItem => ({
  id: "same-id", type: "entry", title, content: "", tags: [], created_at: "", updated_at: "",
})
const pagination = { page: 1, page_size: 20, total_count: 1, total_pages: 1, has_next: false, has_previous: false }
const knowledge = (label: string) => ({ results: [entry(label)], pagination })
let store: ReturnType<typeof createAppStore>
const state = () => store.getState()
const switchTo = (id: string) => state().domainActions.setActiveDomain(id)
beforeEach(() => { store = createAppStore(); switchTo("a") })
afterEach(() => vi.restoreAllMocks())

const lists = [
  { name: "datasets", start: () => state().datasetActions.fetchDatasets(), response: catalog,
    read: () => ({ catalog: state().datasetCatalog, selected: state().selectedDataset, status: state().datasetStatus, error: state().datasetError }) },
  { name: "recipes", start: () => state().recipeActions.fetchRecipes(), response: (label: string) => [recipe(label)],
    read: () => ({ recipes: state().recipes, status: state().recipeStatus, error: state().recipeError }) },
  { name: "knowledge", start: () => state().knowledgeActions.fetchKnowledge(), response: knowledge,
    read: () => ({ items: state().knowledgeItems, pagination: state().knowledgePagination, status: state().knowledgeStatus, error: state().knowledgeError }) },
]
for (const list of lists) {
  it.each(["success", "error"])(`ignores old workspace ${list.name} list %s after B has loaded`, async (outcome) => {
    const old = deferred()
    vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never).mockResolvedValue(list.response("b"))
    const pending = list.start()
    switchTo("b")
    await list.start()
    const expected = list.read()
    if (outcome === "success") old.resolve(list.response("a"))
    else old.reject(new ApiError(503, "old workspace unavailable"))
    await pending
    expect(list.read()).toEqual(expected)
  })
}
it.each(["success", "error"])("ignores stale dataset detail %s after A→B→A", async (outcome) => {
  const old = deferred()
  vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never).mockResolvedValue({ model, dataset: dataset("fresh a") })
  const pending = state().datasetActions.fetchDataset("records")
  switchTo("b")
  switchTo("a")
  await state().datasetActions.fetchDataset("records")
  if (outcome === "success") old.resolve({ model, dataset: dataset("old a") })
  else old.reject(new Error("obsolete detail failure"))
  await pending
  expect(state().selectedDataset).toEqual(dataset("fresh a"))
  expect(state().selectedDatasetStatus).toBe("loaded")
  expect(state().selectedDatasetError).toBeNull()
})
it.each(["success", "error"])("ignores stale recipe detail %s while preserving the caller's result", async (outcome) => {
  const old = deferred()
  vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never).mockResolvedValue(recipe("b"))
  const pending = state().recipeActions.fetchRecipe("same-id")
  switchTo("b")
  await state().recipeActions.fetchRecipe("same-id")
  if (outcome === "success") {
    old.resolve(recipe("a"))
    expect(await pending).toEqual(recipe("a"))
  } else {
    const error = new Error("obsolete recipe failure")
    const rejected = expect(pending).rejects.toBe(error)
    old.reject(error)
    await rejected
  }
  expect(state().currentRecipe).toEqual(recipe("b"))
})
it.each(["success", "error"])("ignores stale recipe runs %s", async (outcome) => {
  const old = deferred()
  vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never).mockResolvedValue([run("b")])
  const pending = state().recipeActions.fetchRuns("same-id")
  switchTo("b")
  await state().recipeActions.fetchRuns("same-id")
  if (outcome === "success") old.resolve([run("a")])
  else old.reject(new Error("obsolete run failure"))
  await pending
  expect(state().recipeRuns).toEqual([run("b")])
})
type Mutation = { name: string; method: "post" | "put" | "patch" | "delete"; start: (store: AppStore) => Promise<unknown>; response: unknown }
const mutations: Mutation[] = [
  { name: "recipe update", method: "put", start: s => s.recipeActions.updateRecipe("same-id", { name: "a" }), response: recipe("a") },
  { name: "recipe delete", method: "delete", start: s => s.recipeActions.deleteRecipe("same-id"), response: undefined },
  { name: "recipe execution", method: "post", start: s => s.recipeActions.runRecipe("same-id", {}), response: run("a") },
  { name: "run sharing", method: "patch", start: s => s.recipeActions.updateRecipeRun("same-id", "same-id", { is_shared: true }), response: { ...run("a"), is_shared: true } },
  { name: "knowledge creation", method: "post", start: s => s.knowledgeActions.createKnowledge({ type: "entry", title: "a" }), response: entry("a") },
  { name: "knowledge update", method: "put", start: s => s.knowledgeActions.updateKnowledge("same-id", { title: "a" }), response: entry("a") },
  { name: "knowledge delete", method: "delete", start: s => s.knowledgeActions.deleteKnowledge("same-id"), response: undefined },
]
for (const mutation of mutations) {
  it(`does not apply old ${mutation.name} to B's matching records`, async () => {
    const old = deferred()
    vi.spyOn(api, mutation.method).mockImplementation(() => old.promise as never)
    const pending = mutation.start(state())
    switchTo("b")
    store.setState({ recipes: [recipe("b")], currentRecipe: recipe("b"), recipeRuns: [run("b")], knowledgeItems: [entry("b")] })
    old.resolve(mutation.response)
    expect(await pending).toEqual(mutation.response)
    expect(state().recipes).toEqual([recipe("b")])
    expect(state().currentRecipe).toEqual(recipe("b"))
    expect(state().recipeRuns).toEqual([run("b")])
    expect(state().knowledgeItems).toEqual([entry("b")])
  })
}
it("does not start a knowledge refresh when an old workspace upload completes", async () => {
  const old = deferred()
  vi.spyOn(api, "upload").mockImplementationOnce(() => old.promise as never)
  const get = vi.spyOn(api, "get").mockResolvedValue(knowledge("unexpected"))
  const pending = state().knowledgeActions.importKnowledge(new File(["data"], "knowledge.zip"))
  switchTo("b")
  store.setState({ knowledgeItems: [entry("b")], knowledgeStatus: "loaded", knowledgePagination: pagination })
  old.resolve({})
  await pending
  expect(get).not.toHaveBeenCalled()
  expect(state().knowledgeItems).toEqual([entry("b")])
  expect(state().knowledgeStatus).toBe("loaded")
})
it("ignores an import-triggered list response arriving after a workspace switch", async () => {
  const old = deferred()
  vi.spyOn(api, "upload").mockResolvedValue({})
  const get = vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never)
  const pending = state().knowledgeActions.importKnowledge(new File(["data"], "knowledge.zip"))
  await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(1))
  switchTo("b")
  store.setState({ knowledgeItems: [entry("b")], knowledgeStatus: "loaded", knowledgePagination: pagination })
  old.resolve(knowledge("a"))
  await pending
  expect(state().knowledgeItems).toEqual([entry("b")])
  expect(state().knowledgePagination).toEqual(pagination)
})

for (const list of lists) {
  it(`does not let an older ${list.name} list finish a newer request's loading state`, async () => {
    const old = deferred(), latest = deferred()
    vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never)
      .mockImplementationOnce(() => latest.promise as never)
    const first = list.start(), second = list.start()
    old.resolve(list.response("obsolete"))
    await first
    expect(list.read().status).toBe("loading")
    latest.resolve(list.response("latest"))
    await second
    expect(list.read().status).toBe("loaded")
  })
}

it.each(["success", "error"])("catalog %s cannot replace a more recent dataset selection", async (outcome) => {
  const old = deferred()
  vi.spyOn(api, "get").mockImplementationOnce(() => old.promise as never)
    .mockResolvedValue({ model, dataset: dataset("chosen detail") })
  const pending = state().datasetActions.fetchDatasets()
  await state().datasetActions.fetchDataset("records")
  if (outcome === "success") old.resolve(catalog("catalog default"))
  else old.reject(new Error("catalog failed"))
  await pending
  expect(state().selectedDataset).toEqual(dataset("chosen detail"))
  expect(state().selectedDatasetStatus).toBe("loaded")
  expect(state().selectedDatasetError).toBeNull()
})

it("clearDatasets invalidates both pending catalog and detail requests", async () => {
  const catalogResponse = deferred(), detailResponse = deferred()
  vi.spyOn(api, "get").mockImplementationOnce(() => catalogResponse.promise as never)
    .mockImplementationOnce(() => detailResponse.promise as never)
  const list = state().datasetActions.fetchDatasets()
  const detail = state().datasetActions.fetchDataset("records")
  state().datasetActions.clearDatasets()
  catalogResponse.resolve(catalog("obsolete"))
  detailResponse.resolve({ model, dataset: dataset("obsolete detail") })
  await Promise.all([list, detail])
  expect(state().datasetCatalog).toBeNull()
  expect(state().selectedDataset).toBeNull()
  expect(state().datasetStatus).toBe("idle")
  expect(state().selectedDatasetStatus).toBe("idle")
})

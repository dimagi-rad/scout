import { render, screen } from "@testing-library/react"
import { beforeEach, expect, it, vi } from "vitest"
import { DataDictionaryPage } from "./DataDictionaryPage"

const { state, network } = vi.hoisted(() => ({
  network: { status: "online" },
  state: {
    dataDictionary: { schemas: {} } as { schemas: Record<string, unknown> } | null,
    dictionaryStatus: "loaded",
    dictionaryWarning: "Some sources could not be refreshed: source B needs operator recovery." as string | null,
    dictionaryError: null as string | null,
    selectedTable: null,
    activeDomainId: "workspace-a",
    dictionaryActions: {
      fetchDictionary: vi.fn(), refreshSchema: vi.fn(), fetchTable: vi.fn(), clearDictionary: vi.fn(),
    },
  },
}))
vi.mock("@/store/store", () => ({ useAppStore: (selector: (value: typeof state) => unknown) => selector(state) }))
vi.mock("@/hooks/useNetworkStatus", () => ({ useNetworkStatus: () => network }))
vi.mock("./SchemaTree", () => ({ SchemaTree: () => <div>Available tables</div> }))
vi.mock("./TableDetail", () => ({ TableDetail: () => null }))

beforeEach(() => {
  state.dataDictionary = { schemas: {} }
  state.dictionaryStatus = "loaded"
  state.dictionaryWarning = "Some sources could not be refreshed: source B needs operator recovery."
  state.dictionaryError = null
  network.status = "online"
})

it("shows a partial refresh warning alongside the usable dictionary", () => {
  render(<DataDictionaryPage />)
  expect(screen.getByRole("status")).toHaveTextContent("source B needs operator recovery")
  expect(screen.getByText("Available tables")).toBeVisible()
  expect(screen.getByTestId("refresh-schema-btn")).toBeEnabled()
  expect(screen.queryByText("Failed to load dictionary")).not.toBeInTheDocument()
})

it("shows partial refresh guidance even before dictionary data becomes available", () => {
  state.dictionaryStatus = "not_materialized"
  render(<DataDictionaryPage />)
  expect(screen.getByTestId("dictionary-empty-state")).toHaveTextContent("source B needs operator recovery")
})


it("does not label an offline dictionary failure as a partial refresh warning", () => {
  network.status = "offline"
  state.dictionaryStatus = "error"
  state.dictionaryWarning = null
  state.dictionaryError = "Failed to fetch"
  render(<DataDictionaryPage />)
  expect(screen.queryByTestId("refresh-schema-warning")).not.toBeInTheDocument()
})


it("keeps normal first-load errors out of onboarding guidance", () => {
  state.dictionaryStatus = "not_materialized"
  state.dictionaryWarning = null
  state.dictionaryError = "Data unavailable. Please refresh workspace data."
  render(<DataDictionaryPage />)
  expect(screen.getByText("Start a chat to automatically fetch your schema data.")).toBeVisible()
  expect(screen.queryByText(state.dictionaryError)).not.toBeInTheDocument()
})

// B2: the server's reason was stored but never rendered, so every refusal read as a generic failure.
it("keeps the loaded dictionary and shows the server's reason when a refresh is refused", () => {
  state.dictionaryStatus = "error"
  state.dictionaryWarning = null
  state.dictionaryError = "A refresh is already in progress."
  render(<DataDictionaryPage />)
  expect(screen.getByTestId("refresh-schema-error")).toHaveTextContent("A refresh is already in progress.")
  expect(screen.getByText("Available tables")).toBeVisible()
  expect(screen.getByTestId("refresh-schema-btn")).toBeEnabled()
  expect(screen.queryByText("Failed to load dictionary")).not.toBeInTheDocument()
})

it("shows the server's reason when the dictionary itself fails to load", () => {
  state.dataDictionary = null
  state.dictionaryStatus = "error"
  state.dictionaryWarning = null
  state.dictionaryError = "No active domain selected."
  render(<DataDictionaryPage />)
  expect(screen.getByText("Failed to load dictionary")).toBeVisible()
  expect(screen.getByTestId("dictionary-error-message")).toHaveTextContent("No active domain selected.")
})

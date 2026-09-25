import { render, screen } from "@testing-library/react"
import { expect, it, vi } from "vitest"
import { DataDictionaryPage } from "./DataDictionaryPage"

const { state } = vi.hoisted(() => ({
  state: {
    dataDictionary: { schemas: {} },
    dictionaryStatus: "loaded",
    dictionaryError: "Some sources could not be refreshed: source B needs operator recovery.",
    selectedTable: null,
    activeDomainId: "workspace-a",
    dictionaryActions: {
      fetchDictionary: vi.fn(), refreshSchema: vi.fn(), fetchTable: vi.fn(), clearDictionary: vi.fn(),
    },
  },
}))
vi.mock("@/store/store", () => ({ useAppStore: (selector: (value: typeof state) => unknown) => selector(state) }))
vi.mock("@/hooks/useNetworkStatus", () => ({ useNetworkStatus: () => ({ status: "online" }) }))
vi.mock("./SchemaTree", () => ({ SchemaTree: () => <div>Available tables</div> }))
vi.mock("./TableDetail", () => ({ TableDetail: () => null }))

it("shows a partial refresh warning alongside the usable dictionary", () => {
  render(<DataDictionaryPage />)
  expect(screen.getByRole("status")).toHaveTextContent("source B needs operator recovery")
  expect(screen.getByText("Available tables")).toBeVisible()
  expect(screen.getByTestId("refresh-schema-btn")).toBeEnabled()
  expect(screen.queryByText("Failed to load dictionary")).not.toBeInTheDocument()
})

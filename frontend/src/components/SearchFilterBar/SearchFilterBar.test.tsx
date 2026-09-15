import { fireEvent, render, screen } from "@testing-library/react"
import { expect, it, vi } from "vitest"
import { SearchFilterBar } from "./SearchFilterBar"

it("only cancels non-composing Enter in the search input", () => {
  render(<SearchFilterBar search="" onSearchChange={vi.fn()} filters={[]} activeFilters={{}} onFilterChange={vi.fn()} />)
  const input = screen.getByTestId("search-filter-input")
  expect(fireEvent.keyDown(input, { key: "Enter" })).toBe(false)
  expect(fireEvent.keyDown(input, { key: "Enter", isComposing: true })).toBe(true)
  expect(fireEvent.keyDown(input, { key: "Enter", keyCode: 229 })).toBe(true)
  expect(fireEvent.keyDown(input, { key: "ArrowDown" })).toBe(true)
})

import { useState } from "react"
import { fireEvent, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { expect, it } from "vitest"

import { computeFacetOptions, type FacetDef, type FacetSelection } from "@/lib/filters/facets"
import { FACET_COLLAPSED_LIMIT, FacetFilterBar } from "./FacetFilterBar"

interface Item {
  org: string
}

const facets: FacetDef<Item>[] = [
  { key: "org", label: "Organization", shortLabel: "Org", searchable: true, getValue: (i) => i.org },
]
const items: Item[] = Array.from({ length: 20 }, (_, n) => ({ org: `org-${String(n).padStart(2, "0")}` }))

function Harness() {
  const [selection, setSelection] = useState<FacetSelection>({})
  return (
    <FacetFilterBar
      testIdPrefix="t"
      search=""
      onSearchChange={() => {}}
      facets={facets}
      options={computeFacetOptions(items, facets, selection)}
      selection={selection}
      onFacetChange={(key, values) => setSelection({ ...selection, [key]: values })}
      onClear={() => setSelection({})}
      shownCount={items.length}
      totalCount={items.length}
    />
  )
}

function optionCount() {
  return screen.getByTestId("t-facet-org-popover").querySelectorAll("input[type=checkbox]").length
}

it("cuts long lists off behind Show more and searches them", async () => {
  const user = userEvent.setup()
  render(<Harness />)
  await user.click(screen.getByTestId("t-facet-org"))

  expect(optionCount()).toBe(FACET_COLLAPSED_LIMIT)
  await user.click(screen.getByTestId("t-facet-org-show-more"))
  expect(optionCount()).toBe(20)

  await user.type(screen.getByTestId("t-facet-org-search"), "org-1")
  expect(optionCount()).toBe(10)
})

it("swallows Enter in the popover so it cannot submit a surrounding form", async () => {
  const user = userEvent.setup()
  render(<Harness />)
  await user.click(screen.getByTestId("t-facet-org"))
  expect(fireEvent.keyDown(screen.getByTestId("t-facet-org-option-org-00"), { key: "Enter" })).toBe(false)
  expect(fireEvent.keyDown(screen.getByTestId("t-facet-org-search"), { key: "Enter" })).toBe(false)
})

it("keeps a value ticked during a search listed after the search clears", async () => {
  const user = userEvent.setup()
  render(<Harness />)
  await user.click(screen.getByTestId("t-facet-org"))
  await user.type(screen.getByTestId("t-facet-org-search"), "org-19")
  await user.click(screen.getByTestId("t-facet-org-option-org-19"))
  await user.clear(screen.getByTestId("t-facet-org-search"))
  expect(screen.getByTestId("t-facet-org-option-org-19")).toBeChecked()
})

it("keeps a selection beyond the cut-off listed when reopened, and labels the button", async () => {
  const user = userEvent.setup()
  render(<Harness />)
  await user.click(screen.getByTestId("t-facet-org"))
  await user.click(screen.getByTestId("t-facet-org-show-more"))
  await user.click(screen.getByTestId("t-facet-org-option-org-19"))
  expect(screen.getByTestId("t-facet-org")).toHaveTextContent("Org: org-19")

  await user.keyboard("{Escape}")
  await user.click(screen.getByTestId("t-facet-org"))

  expect(optionCount()).toBe(FACET_COLLAPSED_LIMIT + 1)
  expect(screen.getByTestId("t-facet-org-option-org-19")).toBeChecked()
  await user.click(screen.getByTestId("t-facet-org-option-org-00"))
  expect(screen.getByTestId("t-facet-org")).toHaveTextContent("Org: 2")
})

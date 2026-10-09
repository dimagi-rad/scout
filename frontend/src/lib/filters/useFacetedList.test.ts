import { act, renderHook } from "@testing-library/react"
import { beforeEach, expect, it } from "vitest"

import type { FacetDef } from "./facets"
import { useFacetedList } from "./useFacetedList"

interface Row {
  org: string
}

const facets: FacetDef<Row>[] = [{ key: "org", label: "Org", getValue: (r) => r.org }]
const KEY = "scout:test-faceted"
const always = () => true

function stored() {
  return JSON.parse(localStorage.getItem(KEY) ?? "{}")
}

beforeEach(() => localStorage.clear())

it("keeps stored values this list lacks when toggling, and drops them on replace", () => {
  // e.g. the add-source panel, whose list excludes sources already in the workspace.
  localStorage.setItem(KEY, JSON.stringify({ org: ["acme", "dimagi"] }))
  const items = [{ org: "dimagi" }, { org: "foo" }]
  const { result } = renderHook(() =>
    useFacetedList({ items, facets, storageKey: KEY, predicate: always }),
  )
  expect(result.current.selection).toEqual({ org: ["dimagi"] })

  act(() => result.current.setFacet("org", ["dimagi", "foo"]))
  expect(stored()).toEqual({ org: ["dimagi", "foo", "acme"] })

  act(() => result.current.setFacet("org", ["foo"], true))
  expect(stored()).toEqual({ org: ["foo"] })
})

it("keeps two pickers on the same key in sync", () => {
  const items = [{ org: "dimagi" }, { org: "foo" }]
  const a = renderHook(() => useFacetedList({ items, facets, storageKey: KEY, predicate: always }))
  const b = renderHook(() => useFacetedList({ items, facets, storageKey: KEY, predicate: always }))

  act(() => a.result.current.setFacet("org", ["foo"]))

  expect(b.result.current.selection).toEqual({ org: ["foo"] })
  expect(b.result.current.filtered).toEqual([{ org: "foo" }])
})

it("follows writes from another tab", () => {
  const items = [{ org: "dimagi" }, { org: "foo" }]
  const { result } = renderHook(() =>
    useFacetedList({ items, facets, storageKey: KEY, predicate: always }),
  )
  act(() => {
    localStorage.setItem(KEY, JSON.stringify({ org: ["dimagi"] }))
    window.dispatchEvent(new StorageEvent("storage", { key: KEY }))
  })
  expect(result.current.selection).toEqual({ org: ["dimagi"] })
})

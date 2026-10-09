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

it("clears only what this list offers", () => {
  localStorage.setItem(KEY, JSON.stringify({ org: ["acme", "dimagi"], gone: ["x"] }))
  const facetsWithGone: FacetDef<Row>[] = [
    ...facets,
    { key: "gone", label: "Gone", getValue: () => undefined },
  ]
  const items = [{ org: "dimagi" }]
  const { result } = renderHook(() =>
    useFacetedList({ items, facets: facetsWithGone, storageKey: KEY, predicate: always }),
  )
  act(() => result.current.clearFacets())
  expect(result.current.selection).toEqual({})
  expect(stored()).toEqual({ org: ["acme"], gone: ["x"] })
})

it("lets a partial picker reset a facet without wiping the complete picker's values", () => {
  localStorage.setItem(KEY, JSON.stringify({ org: ["acme", "foo"] }))
  const all = [{ org: "acme" }, { org: "foo" }, { org: "bar" }]
  const partial = [{ org: "foo" }, { org: "bar" }]
  const complete = renderHook(() =>
    useFacetedList({ items: all, facets, storageKey: KEY, predicate: always, listIsComplete: true }),
  )
  const panel = renderHook(() =>
    useFacetedList({ items: partial, facets, storageKey: KEY, predicate: always }),
  )
  expect(panel.result.current.selection).toEqual({ org: ["foo"] })

  act(() => panel.result.current.setFacet("org", []))

  expect(complete.result.current.selection).toEqual({ org: ["acme"] })
  expect(panel.result.current.selection).toEqual({})
})

it("drops values a complete list lacks, so revoked ones cannot linger", () => {
  localStorage.setItem(KEY, JSON.stringify({ org: ["revoked", "foo"] }))
  const items = [{ org: "foo" }, { org: "bar" }]
  const { result } = renderHook(() =>
    useFacetedList({ items, facets, storageKey: KEY, predicate: always, listIsComplete: true }),
  )
  act(() => result.current.setFacet("org", ["foo", "bar"]))
  expect(stored()).toEqual({ org: ["foo", "bar"] })

  act(() => {
    localStorage.setItem(KEY, JSON.stringify({ org: ["revoked", "foo"] }))
    window.dispatchEvent(new StorageEvent("storage", { key: KEY }))
  })
  act(() => result.current.clearFacets())
  expect(localStorage.getItem(KEY)).toBeNull()
})

it("clears only offered facets on a complete list, keeping hidden ones", () => {
  localStorage.setItem(KEY, JSON.stringify({ org: ["foo"], gone: ["x"] }))
  const facetsWithGone: FacetDef<Row>[] = [
    ...facets,
    { key: "gone", label: "Gone", getValue: () => undefined },
  ]
  const { result } = renderHook(() =>
    useFacetedList({
      items: [{ org: "foo" }],
      facets: facetsWithGone,
      storageKey: KEY,
      predicate: always,
      listIsComplete: true,
    }),
  )
  act(() => result.current.clearFacets())
  expect(stored()).toEqual({ gone: ["x"] })
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

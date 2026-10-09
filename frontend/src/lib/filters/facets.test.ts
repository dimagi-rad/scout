import { describe, expect, it } from "vitest"

import {
  applyFacets,
  availableFacets,
  computeFacetOptions,
  effectiveSelection,
  isFiltering,
  type FacetDef,
} from "./facets"

interface Row {
  name: string
  kind: "a" | "b"
  color?: string
  size?: string
}

const facets: FacetDef<Row>[] = [
  { key: "kind", label: "Kind", getValue: (r) => r.kind },
  // Only "a" rows have a color; "b" rows are not applicable, like Connect facets for CommCare.
  {
    key: "color",
    label: "Color",
    getValue: (r) => (r.kind === "a" ? (r.color ?? "unknown") : undefined),
    valueOrder: ["red", "blue", "unknown"],
  },
  {
    key: "size",
    label: "Size",
    getValue: (r) => (r.kind === "a" ? (r.size ?? "none") : undefined),
    trailingValues: ["none"],
    optionLabel: (v) => (v === "none" ? "None" : v.toUpperCase()),
  },
]

const rows: Row[] = [
  { name: "a1", kind: "a", color: "red", size: "s" },
  { name: "a2", kind: "a", color: "blue", size: "l" },
  { name: "a3", kind: "a", color: "red" },
  { name: "a4", kind: "a", size: "s" },
  { name: "b1", kind: "b" },
  { name: "b2", kind: "b" },
]

const names = (items: Row[]) => items.map((r) => r.name)

describe("applyFacets", () => {
  it("keeps everything with no selection", () => {
    expect(names(applyFacets(rows, facets, {}))).toEqual(names(rows))
    expect(names(applyFacets(rows, facets, { color: [] }))).toEqual(names(rows))
  })

  it("ORs values within a facet", () => {
    expect(names(applyFacets(rows, facets, { color: ["red", "blue"], kind: ["a"] }))).toEqual([
      "a1",
      "a2",
      "a3",
    ])
  })

  it("ANDs across facets", () => {
    expect(names(applyFacets(rows, facets, { color: ["red"], size: ["s"] }))).toEqual([
      "a1",
      "b1",
      "b2",
    ])
  })

  it("passes rows the facet does not apply to", () => {
    expect(names(applyFacets(rows, facets, { color: ["blue"] }))).toEqual(["a2", "b1", "b2"])
  })

  it("matches Unknown and None values like any other", () => {
    expect(names(applyFacets(rows, facets, { color: ["unknown"], kind: ["a"] }))).toEqual(["a4"])
    expect(names(applyFacets(rows, facets, { size: ["none"], kind: ["a"] }))).toEqual(["a3"])
  })

  it("combines with a predicate such as the text search", () => {
    const predicate = (r: Row) => r.name.endsWith("1")
    expect(names(applyFacets(rows, facets, { kind: ["a"] }, predicate))).toEqual(["a1"])
  })
})

describe("computeFacetOptions", () => {
  it("counts each facet against the predicate and the OTHER facets only", () => {
    const options = computeFacetOptions(rows, facets, { color: ["red"], kind: ["a"] })
    // Kind counts ignore the kind selection but honour color=red ("b" rows pass color).
    expect(options.kind).toEqual([
      { value: "a", label: "a", count: 2 },
      { value: "b", label: "b", count: 2 },
    ])
    // Color counts ignore the color selection but honour kind=a.
    expect(options.color).toEqual([
      { value: "red", label: "red", count: 2 },
      { value: "blue", label: "blue", count: 1 },
      { value: "unknown", label: "unknown", count: 1 },
    ])
    expect(options.size.map((o) => [o.value, o.count])).toEqual([
      ["l", 0],
      ["s", 1],
      ["none", 1],
    ])
  })

  it("applies the predicate to counts and keeps zero-count values listed", () => {
    const options = computeFacetOptions(rows, facets, {}, (r) => r.name === "a2")
    expect(options.color.map((o) => [o.value, o.count])).toEqual([
      ["red", 0],
      ["blue", 1],
      ["unknown", 0],
    ])
  })

  it("labels options and sorts trailing values last", () => {
    const options = computeFacetOptions(rows, facets, {})
    expect(options.size.map((o) => o.label)).toEqual(["L", "S", "None"])
  })
})

describe("effectiveSelection", () => {
  it("drops values no item has and facets not offered", () => {
    const offered = facets.slice(0, 2)
    expect(
      effectiveSelection({ color: ["red", "green"], size: ["s"], kind: ["c"] }, offered, rows),
    ).toEqual({ color: ["red"] })
  })
})

describe("availableFacets", () => {
  it("hides facets no item has a value for unless isAvailable says otherwise", () => {
    const onlyB = rows.filter((r) => r.kind === "b")
    expect(availableFacets(facets, onlyB).map((f) => f.key)).toEqual(["kind"])
    const custom: FacetDef<Row> = { ...facets[0], isAvailable: () => false }
    expect(availableFacets([custom], rows)).toEqual([])
  })
})

it("isFiltering ignores empty selections", () => {
  expect(isFiltering({})).toBe(false)
  expect(isFiltering({ kind: [] })).toBe(false)
  expect(isFiltering({ kind: ["a"] })).toBe(true)
})
